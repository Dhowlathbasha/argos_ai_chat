"""
agents/argos_agent.py — Playwright adapter for Argos 'Ask Trevor' AI Chat
────────────────────────────────────────────────────────────────────────
Automates end-to-end interactions with the Ask Trevor AI shopping assistant
on https://www.argos.co.uk/ using Playwright.

Features:
- Normal headed Chrome execution with coherent UK browser settings
- Robust cookie consent auto-handling (OneTrust & fallback prompts)
- Iframe-aware interaction (#trevor-iframe-embed inside dialog#trevor-drawer)
- SSE network stream detection (/trevor/api/chat) with DOM stabilization fallback
- Visual evidence capture (full page & chat drawer screenshots)
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
import time
from datetime import datetime
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, FrameLocator, Page, async_playwright

ARGOS_URL = os.getenv("ARGOS_BASE_URL", "https://www.argos.co.uk/")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")
USE_PERSISTENT_CONTEXT = (
    os.getenv("USE_PERSISTENT_CONTEXT", "true").strip().lower() == "true"
)
USE_CDP_CHROME = os.getenv("USE_CDP_CHROME", "false").strip().lower() == "true"
PLAYWRIGHT_PROFILE_DIR = os.getenv("PLAYWRIGHT_PROFILE_DIR", ".playwright-profiles")
CHROME_EXECUTABLE = os.getenv("CHROME_EXECUTABLE", "").strip()
CHROME_REMOTE_DEBUGGING_PORT = int(os.getenv("CHROME_REMOTE_DEBUGGING_PORT", "9223"))
DEFAULT_TIMEOUT_MS = 25000
RESPONSE_TIMEOUT_S = float(os.getenv("TREVOR_RESPONSE_TIMEOUT_S", "45"))
BROWSER_START_STAGGER_S = float(os.getenv("BROWSER_START_STAGGER_S", "0"))
ARGOS_NAVIGATION_RETRIES = int(os.getenv("ARGOS_NAVIGATION_RETRIES", "1"))
ARGOS_BLOCK_BACKOFF_S = float(os.getenv("ARGOS_BLOCK_BACKOFF_S", "20"))
PLAYWRIGHT_TEARDOWN_TIMEOUT_S = float(
    os.getenv("PLAYWRIGHT_TEARDOWN_TIMEOUT_S", "15")
)

# Playwright adds both switches to Chromium's default command line. Chrome does
# not need --no-sandbox on a normal macOS workstation, and --enable-automation
# makes an otherwise stock installed-Chrome session trivially distinguishable
# from the successful manual incognito control test. Omit these launch switches;
# do not inject navigator or DOM spoofing scripts.
CHROME_IGNORED_DEFAULT_ARGS = ["--no-sandbox", "--enable-automation"]


class ArgosAccessDeniedError(RuntimeError):
    """Raised when Argos/Akamai rejects the browser navigation."""


class ArgosTrevorAgent:
    """Playwright adapter for Argos 'Ask Trevor' AI Shopping Assistant."""

    def __init__(
        self,
        evidence: Any = None,
        headless: bool = HEADLESS,
        channel: Optional[str] = BROWSER_CHANNEL,
        video_dir: Optional[Any] = None,
        persistent_context: bool = USE_PERSISTENT_CONTEXT,
        profile_dir: Optional[Any] = None,
        cdp_chrome: bool = USE_CDP_CHROME,
    ):
        self.headless = headless
        self.channel = channel
        self.evidence = evidence
        self.video_dir = Path(video_dir) if video_dir else None
        self.persistent_context = persistent_context
        self.cdp_chrome = cdp_chrome
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._active_profile_dir: Optional[Path] = None
        self._recorded_video_path: Optional[Path] = None
        self._playwright = None
        self._browser: Browser | None = None
        self._chrome_process: asyncio.subprocess.Process | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._frame: FrameLocator | None = None
        self.api_events: list[dict] = []
        self._stream_complete_event = asyncio.Event()

    async def setup(self) -> None:
        """Launch browser, handle cookies, open Ask Trevor drawer, and prepare chat."""
        print("[ArgosTrevorAgent] Starting Playwright...", flush=True)
        self._playwright = await async_playwright().start()

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-GB",
            "timezone_id": "Europe/London",
        }
        if self.video_dir:
            self.video_dir.mkdir(parents=True, exist_ok=True)
            context_kwargs["record_video_dir"] = str(self.video_dir)
            context_kwargs["record_video_size"] = {"width": 1440, "height": 900}

        await self._launch_browser_context(context_kwargs)
        existing_pages = self._context.pages
        self._page = existing_pages[0] if existing_pages else await self._context.new_page()
        self._setup_network_listeners()

        # Optional jitter for environments where simultaneous workers trigger
        # rate limiting. It is disabled by default so cases start immediately.
        if BROWSER_START_STAGGER_S > 0:
            import random

            await asyncio.sleep(random.uniform(0, BROWSER_START_STAGGER_S))

        # 1. Navigate to Argos
        print(f"[ArgosTrevorAgent] Navigating to {ARGOS_URL}...", flush=True)
        await self._navigate_to_argos()

        # 2. Handle Cookie Popups
        await self.dismiss_cookie_popups()

        # 3. Open Ask Trevor Drawer
        await self._open_trevor_drawer()

        # 4. Resolve Trevor Iframe
        await self._init_trevor_frame()
        print("[ArgosTrevorAgent] Setup complete.", flush=True)

    def _persistent_profile_path(self) -> Path:
        """Return a stable, worker-isolated Chrome profile directory."""
        profile_root = self.profile_dir or Path(PLAYWRIGHT_PROFILE_DIR)
        if not profile_root.is_absolute():
            profile_root = Path(__file__).resolve().parent.parent / profile_root

        worker = os.getenv("PYTEST_XDIST_WORKER", "main")
        safe_worker = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in worker
        ) or "main"
        return profile_root / safe_worker

    async def _launch_browser_context(self, context_kwargs: dict[str, Any]) -> None:
        """Launch either a persistent Chrome context or the legacy isolated context."""
        if self.cdp_chrome:
            await self._launch_stock_chrome_over_cdp(context_kwargs)
            return

        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "ignore_default_args": CHROME_IGNORED_DEFAULT_ARGS,
        }
        if self.channel:
            launch_kwargs["channel"] = self.channel

        if self.persistent_context:
            self._active_profile_dir = self._persistent_profile_path()
            self._active_profile_dir.mkdir(parents=True, exist_ok=True)
            print(
                "[ArgosTrevorAgent] Launching persistent "
                f"{self.channel or 'chromium'} profile: {self._active_profile_dir}",
                flush=True,
            )
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._active_profile_dir),
                    **launch_kwargs,
                    **context_kwargs,
                )
            except Exception as exc:
                raise RuntimeError(
                    "Persistent Chrome context failed to start. Ensure no other "
                    f"process is using profile '{self._active_profile_dir}'."
                ) from exc
            return

        try:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            if not self.channel:
                raise
            launch_kwargs.pop("channel", None)
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(**context_kwargs)

    def _chrome_executable(self) -> str:
        """Resolve an installed stable Chrome executable, preferring explicit config."""
        candidates = [
            CHROME_EXECUTABLE,
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
            shutil.which("chrome") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        raise RuntimeError(
            "USE_CDP_CHROME=true requires an installed Google Chrome. Set "
            "CHROME_EXECUTABLE to its full path."
        )

    def _cdp_port(self) -> int:
        """Give each xdist worker a deterministic local DevTools port."""
        worker = os.getenv("PYTEST_XDIST_WORKER", "main")
        match = re.search(r"(\d+)$", worker)
        return CHROME_REMOTE_DEBUGGING_PORT + (int(match.group(1)) if match else 0)

    async def _launch_stock_chrome_over_cdp(
        self, context_kwargs: dict[str, Any]
    ) -> None:
        """Start stock Chrome and attach Playwright without its blocked launch flags."""
        chrome = self._chrome_executable()
        port = self._cdp_port()
        profile_dir = self._persistent_profile_path().with_name(
            f"cdp-{self._persistent_profile_path().name}"
        )
        profile_dir.mkdir(parents=True, exist_ok=True)

        command = [
            chrome,
            "--incognito",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        if self.headless:
            command.insert(1, "--headless=new")

        print(
            "[ArgosTrevorAgent] Launching stock Chrome incognito and attaching "
            f"over CDP on 127.0.0.1:{port}",
            flush=True,
        )
        self._chrome_process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        endpoint = f"http://127.0.0.1:{port}"
        last_error: Exception | None = None
        for _ in range(80):
            if self._chrome_process.returncode is not None:
                raise RuntimeError(
                    "Stock Chrome exited before its DevTools endpoint became ready."
                )
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint,
                    timeout=1000,
                )
                break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        else:
            raise RuntimeError(
                f"Could not connect to stock Chrome at {endpoint}."
            ) from last_error

        # A new isolated context retains locale, timezone, and video settings,
        # while Chrome itself was launched through its ordinary executable.
        self._context = await self._browser.new_context(**context_kwargs)

    async def _navigate_to_argos(self) -> None:
        """Navigate with one bounded cooldown retry for Akamai 403/429 pages."""
        if self._page is None:
            raise RuntimeError("Browser page has not been created.")

        attempts = max(0, ARGOS_NAVIGATION_RETRIES) + 1
        for attempt in range(attempts):
            response = await self._page.goto(
                ARGOS_URL,
                timeout=45000,
                wait_until="domcontentloaded",
            )
            await self._page.wait_for_timeout(2000)

            if not await self._is_access_denied(response):
                return

            await self._capture_access_denied_diagnostic(attempt + 1)
            if attempt + 1 >= attempts:
                browser_mode = (
                    "persistent headed Chrome"
                    if self.persistent_context
                    else "a fresh incognito-style Chrome context"
                )
                raise ArgosAccessDeniedError(
                    "Argos was blocked by Akamai (HTTP 403/429 or Access Denied). "
                    "The bounded retry was exhausted. Stop the run and allow the "
                    f"IP to cool down; {browser_mode} was already tested. "
                    "If this persists, verify normal Chrome on the same network and "
                    "request an allowlisted test environment from Argos."
                )

            delay = max(0.0, ARGOS_BLOCK_BACKOFF_S) * (2**attempt)
            print(
                "[ArgosTrevorAgent] Akamai block detected; "
                f"cooling down for {delay:g}s before the only retry...",
                flush=True,
            )
            await self._page.wait_for_timeout(int(delay * 1000))

    async def _is_access_denied(self, response: Any) -> bool:
        """Recognize HTTP and rendered Akamai denial responses."""
        status = getattr(response, "status", None)
        if status in {403, 429}:
            return True

        if self._page is None:
            return False

        url = (getattr(self._page, "url", "") or "").lower()
        if "errors.edgesuite.net" in url:
            return True

        try:
            title = (await self._page.title()).lower()
            body = (await self._page.locator("body").inner_text(timeout=2000)).lower()
        except Exception:
            return False

        page_text = f"{title}\n{body}"
        return "access denied" in page_text and (
            "reference #" in page_text or "permission to access" in page_text
        )

    async def _capture_access_denied_diagnostic(self, attempt: int) -> None:
        """Save a non-turn diagnostic only after the denial response is rendered."""
        if self._page is None or self.evidence is None:
            return
        out_dir = getattr(self.evidence, "out_dir", None)
        if out_dir is None:
            return
        try:
            path = Path(out_dir) / f"access_denied_attempt_{attempt}.png"
            await self._page.screenshot(path=str(path), full_page=True)
            print(f"[ArgosTrevorAgent] Access-denied diagnostic: {path}", flush=True)
        except Exception as exc:
            print(f"[ArgosTrevorAgent] Diagnostic screenshot warning: {exc}", flush=True)

    async def dismiss_cookie_popups(self) -> bool:
        """Find and dismiss any cookie consent popups (OneTrust or custom banners)."""
        if self._page is None:
            return False

        dismissed = False
        cookie_selectors = [
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            "#consent_prompt_submit",
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept All Cookies')",
            "button:has-text('Accept all')",
            "button:has-text('Accept Cookies')",
        ]

        for selector in cookie_selectors:
            try:
                locator = self._page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible(timeout=2000):
                    print(f"[ArgosTrevorAgent] Dismissing cookie banner via '{selector}'", flush=True)
                    await locator.first.click(force=True)
                    dismissed = True
                    await self._page.wait_for_timeout(1000)
                    break
            except Exception:
                continue

        # Also remove dark overlay if it intercepts pointer events
        try:
            await self._page.evaluate("""() => {
                const overlay = document.querySelector('.onetrust-pc-dark-filter, #onetrust-consent-sdk');
                if (overlay) {
                    overlay.style.display = 'none';
                }
            }""")
        except Exception:
            pass

        return dismissed

    async def _open_trevor_drawer(self) -> None:
        """Click 'Ask Trevor' button and ensure the drawer dialog opens."""
        print("[ArgosTrevorAgent] Opening Ask Trevor drawer...", flush=True)
        iframe_elem = self._page.locator("#trevor-iframe-embed")
        trevor_btn = self._page.locator("[data-test='ask-trevor-button']")

        for attempt in range(5):
            await self.dismiss_cookie_popups()

            # If iframe is already present, drawer is open!
            if await iframe_elem.count() > 0 and await iframe_elem.first.is_visible():
                print("[ArgosTrevorAgent] Ask Trevor iframe is attached and visible.", flush=True)
                return

            print(f"[ArgosTrevorAgent] Clicking Ask Trevor button (attempt {attempt + 1}/5)...", flush=True)
            try:
                if await trevor_btn.count() > 0:
                    await trevor_btn.first.scroll_into_view_if_needed()
                    await trevor_btn.first.click(timeout=4000)
            except Exception:
                try:
                    await self._page.evaluate("document.querySelector(\"[data-test='ask-trevor-button']\")?.click()")
                except Exception:
                    pass

            # Check if iframe attaches within 4 seconds
            try:
                await iframe_elem.wait_for(state="attached", timeout=4000)
                print(f"[ArgosTrevorAgent] Trevor iframe attached after attempt {attempt + 1}.", flush=True)
                return
            except Exception:
                await self._page.wait_for_timeout(1500)

        # Final fallback wait
        try:
            await iframe_elem.wait_for(state="attached", timeout=15000)
        except Exception:
            pass

    async def _init_trevor_frame(self) -> None:
        """Locate and verify Trevor's embedded iframe and chat textarea."""
        print("[ArgosTrevorAgent] Connecting to Trevor iframe...", flush=True)
        iframe_elem = self._page.locator("#trevor-iframe-embed")

        # Ensure iframe is attached
        if await iframe_elem.count() == 0:
            await self._open_trevor_drawer()

        await iframe_elem.wait_for(state="attached", timeout=25000)
        self._frame = self._page.frame_locator("#trevor-iframe-embed")
        textarea = self._frame.locator("textarea[placeholder*='Trevor'], textarea")

        for attempt in range(4):
            try:
                await textarea.first.wait_for(state="visible", timeout=8000)
                print("[ArgosTrevorAgent] Trevor textarea ready.", flush=True)
                return
            except Exception:
                print(f"[ArgosTrevorAgent] Awaiting Trevor textarea (retry {attempt + 1}/4)...", flush=True)
                await self.dismiss_cookie_popups()
                try:
                    await self._page.evaluate("document.querySelector(\"[data-test='ask-trevor-button']\")?.click()")
                except Exception:
                    pass
                await self._page.wait_for_timeout(2000)

        await textarea.first.wait_for(state="visible", timeout=20000)
        print("[ArgosTrevorAgent] Trevor textarea ready.", flush=True)


    def _setup_network_listeners(self) -> None:
        """Monitor API traffic from Trevor chat."""
        async def on_response(response):
            if "trevor/api/chat" in response.url or "trevor" in response.url:
                try:
                    self.api_events.append({
                        "url": response.url,
                        "status": response.status,
                        "content_type": response.headers.get("content-type", ""),
                        "timestamp": datetime.now().isoformat(),
                    })
                    if "api/chat" in response.url and response.status == 200:
                        # Response headers arrive before streamed content is
                        # necessarily complete. Mark completion only after
                        # Playwright confirms the response body has finished.
                        try:
                            await response.finished()
                        finally:
                            self._stream_complete_event.set()
                except Exception:
                    pass

        self._page.on("response", on_response)

    async def call(self, message: str, *, turn_number: Optional[int] = None) -> str:
        """Submit one user turn, await its completed response, then capture evidence."""
        if self._frame is None:
            raise RuntimeError("ArgosTrevorAgent is not initialized. Run setup() first.")

        await self.dismiss_cookie_popups()

        textarea = self._frame.locator("textarea[placeholder='Ask Trevor a question.']")
        await textarea.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

        # Snapshot Trevor's current state before the user submits anything.
        # This prevents the welcome message from being mistaken for Turn 1.
        previous_reply_count, previous_reply = await self._extract_reply_state()

        turn_label = f"Turn {turn_number}" if turn_number is not None else "User turn"
        print(f"[ArgosTrevorAgent] {turn_label} query: '{message}'", flush=True)
        await textarea.fill(message)
        await self._page.wait_for_timeout(300)

        self._stream_complete_event.clear()
        await textarea.press("Enter")
        print("[ArgosTrevorAgent] Query submitted. Awaiting Trevor response...", flush=True)

        reply = await self._wait_for_reply(
            previous_reply_count=previous_reply_count,
            previous_reply=previous_reply,
        )

        # Give the browser a short render-only settling window after response
        # completion. No screenshot is taken if waiting for the response fails.
        await self._page.wait_for_timeout(500)
        if self.evidence is not None:
            try:
                screenshot = await self._page.screenshot(
                    full_page=True,
                    animations="disabled",
                )
                if hasattr(self.evidence, "record"):
                    self.evidence.record(
                        query=message,
                        response=reply,
                        screenshot_bytes=screenshot,
                        timestamp=datetime.now(),
                        turn_index=turn_number,
                    )
            except Exception as exc:
                print(f"[ArgosTrevorAgent] Screenshot capture warning: {exc}", flush=True)

        return reply

    async def _wait_for_reply(
        self,
        previous_reply_count: int = 0,
        previous_reply: str = "",
        max_wait_sec: float = RESPONSE_TIMEOUT_S,
    ) -> str:
        """Wait for a new Trevor response to arrive and finish rendering."""
        start_time = time.time()
        last_text = ""
        stable_count = 0
        new_response_seen = False

        await self._page.wait_for_timeout(1000)

        while time.time() - start_time < max_wait_sec:
            current_count, current_text = await self._extract_reply_state()

            # When Trevor labels are present, only a new Trevor message counts.
            # Text can change earlier merely because the submitted user query
            # was appended beneath the previous response.
            is_new_response = (
                current_count > previous_reply_count
                if previous_reply_count > 0
                else bool(current_text and current_text != previous_reply)
            )
            if is_new_response:
                new_response_seen = True

            if new_response_seen and current_text:
                if current_text == last_text and len(current_text) > 15:
                    stable_count += 1
                    # Prefer the completed network response, while retaining a
                    # three-poll DOM fallback for streaming/SSE implementations.
                    if (
                        stable_count >= 2 and self._stream_complete_event.is_set()
                    ) or stable_count >= 3:
                        return current_text
                else:
                    stable_count = 0
                    last_text = current_text

            await self._page.wait_for_timeout(1000)

        raise TimeoutError(
            "Trevor did not produce a new, completed response within "
            f"{max_wait_sec:g} seconds; screenshot was not captured."
        )

    async def _extract_reply_state(self) -> tuple[int, str]:
        """Return the number of Trevor messages and the latest response text."""
        if self._frame is None:
            return 0, ""

        try:
            text = await self._frame.locator("main").inner_text()
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            trevor_indices = [
                index for index, line in enumerate(lines) if line.lower() == "trevor"
            ]
            if trevor_indices:
                response_lines = lines[trevor_indices[-1] + 1 :]
                ignored = {
                    "Ask Trevor a question.",
                    "Send message",
                    "Scroll to bottom",
                    "View more information",
                }
                return len(trevor_indices), "\n".join(
                    line for line in response_lines if line not in ignored
                )
            return 0, text.strip()
        except Exception:
            return 0, ""

    async def _extract_latest_trevor_message(self) -> str:
        """Extract the most recent reply from Trevor inside the iframe."""
        _, text = await self._extract_reply_state()
        return text

    async def teardown(self) -> None:
        """Close browser context, flush video recording, and stop Playwright."""
        print("[ArgosTrevorAgent] Tearing down browser...", flush=True)
        video = self._page.video if self._page else None

        async def close_step(label: str, awaitable: Any) -> Any:
            try:
                return await asyncio.wait_for(
                    awaitable,
                    timeout=PLAYWRIGHT_TEARDOWN_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                print(
                    f"[ArgosTrevorAgent] {label} exceeded "
                    f"{PLAYWRIGHT_TEARDOWN_TIMEOUT_S:g}s; continuing cleanup.",
                    flush=True,
                )
            except Exception as exc:
                print(f"[ArgosTrevorAgent] {label} warning: {exc}", flush=True)
            return None

        # Each operation is isolated: one stuck Playwright close must not skip
        # the later cleanup steps or occupy an xdist worker indefinitely.
        if self._page:
            await close_step("Page close", self._page.close())
        if self._context:
            await close_step("Browser context close", self._context.close())

        # Playwright finalizes video only after the page/context closes.
        raw_video_path = None
        if video:
            raw_video_path = await close_step("Video finalization", video.path())
        if raw_video_path and os.path.exists(raw_video_path):
            self._recorded_video_path = Path(raw_video_path)
        elif self.video_dir and self.video_dir.exists():
            webms = sorted(
                self.video_dir.glob("*.webm"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if webms:
                self._recorded_video_path = webms[0]

        if self._browser:
            await close_step("Browser close", self._browser.close())
        if self._chrome_process and self._chrome_process.returncode is None:
            self._chrome_process.terminate()
            await close_step("Stock Chrome process exit", self._chrome_process.wait())
            if self._chrome_process.returncode is None:
                self._chrome_process.kill()
        if self._playwright:
            await close_step("Playwright stop", self._playwright.stop())

        print("[ArgosTrevorAgent] Browser teardown complete.", flush=True)

    def get_recorded_video_path(self) -> Optional[Path]:
        """Return path to the captured WebM video file, if one exists."""
        if self._recorded_video_path and self._recorded_video_path.exists():
            return self._recorded_video_path
        if self.video_dir and self.video_dir.exists():
            webms = sorted(self.video_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
            if webms:
                return webms[0]
        return None
