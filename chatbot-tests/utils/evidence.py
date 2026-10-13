"""
utils/evidence.py — per-turn evidence capture (query, response, screenshot)
and PDF report generation with multi-criteria judging (PASS / FAIL / WARN with PASS)
and execution video (.mp4) referencing for Argos 'Ask Trevor' test scenarios.
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fpdf import FPDF
from PIL import Image, ImageDraw

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

REPORTS_ROOT = Path(__file__).parents[1] / "reports"
EVIDENCE_ROOT = Path(__file__).parents[1] / "evidence"

# Styling tokens for PDF
_NEUTRAL_LABEL_FILL = (240, 242, 245)
_QUERY_HIGHLIGHT_FILL = (235, 243, 255)
_QUERY_HIGHLIGHT_BORDER = (150, 185, 235)
_RESPONSE_HIGHLIGHT_FILL = (255, 248, 230)
_RESPONSE_HIGHLIGHT_BORDER = (235, 195, 90)

_STATUS_COLORS = {
    "PASS": {
        "bg": (220, 245, 220),
        "border": (40, 167, 69),
        "text": (25, 110, 45),
    },
    "WARN with PASS": {
        "bg": (255, 243, 205),
        "border": (240, 173, 78),
        "text": (160, 100, 10),
    },
    "FAIL": {
        "bg": (253, 225, 225),
        "border": (220, 53, 69),
        "text": (165, 20, 30),
    },
}


def _safe(text: str) -> str:
    """Sanitize unicode to Latin-1 compatible characters for FPDF core fonts."""
    return (text or "").encode("latin-1", "replace").decode("latin-1")


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _stamp_screenshot(screenshot_bytes: bytes, ts: datetime, turn_label: str = "") -> bytes:
    """Burn a timestamp and turn banner onto the bottom-left corner of the screenshot."""
    try:
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        label = f"{turn_label} | Captured: {_fmt(ts)}" if turn_label else f"Captured: {_fmt(ts)}"
        draw = ImageDraw.Draw(img, "RGBA")
        text_w = draw.textlength(label)
        pad = 8
        band_h = 26
        y0 = img.height - band_h
        draw.rectangle([0, y0, text_w + 2 * pad, img.height], fill=(0, 0, 0, 180))
        draw.text((pad, y0 + 5), label, fill=(255, 255, 255, 255))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return screenshot_bytes


@dataclass
class Turn:
    index: int
    query: str
    response: str
    screenshot_path: Path
    timestamp: datetime


@dataclass
class EvidenceRecorder:
    scenario_name: str
    case_label: str
    persona: str = ""
    scenario_objective: str = ""
    run_started_at: datetime = field(default_factory=datetime.now)
    turns: list[Turn] = field(default_factory=list)
    video_path: Optional[Path] = None
    _pbar: object = field(default=None, init=False, repr=False, compare=False)

    @property
    def run_id(self) -> str:
        return self.run_started_at.strftime("%Y%m%d_%H%M%S")

    @property
    def out_dir(self) -> Path:
        """Directory under reports/ for storing artifacts of this specific run."""
        d = REPORTS_ROOT / self.scenario_name / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def video_dir(self) -> Path:
        """Playwright target directory for video recordings."""
        v_dir = self.out_dir / "videos"
        v_dir.mkdir(parents=True, exist_ok=True)
        return v_dir

    def record(
        self,
        query: str,
        response: str,
        screenshot_bytes: bytes,
        timestamp: Optional[datetime] = None,
        turn_index: Optional[int] = None,
    ) -> Path:
        """Record a completed query/response turn and its post-response screenshot."""
        ts = timestamp or datetime.now()
        expected_index = len(self.turns) + 1
        idx = turn_index if turn_index is not None else expected_index
        if idx != expected_index:
            raise ValueError(
                f"Evidence turns must be sequential: expected {expected_index}, got {idx}"
            )
        shot_path = self.out_dir / f"turn_{idx:02d}.png"
        turn_label = f"Turn {idx}"
        shot_path.write_bytes(_stamp_screenshot(screenshot_bytes, ts, turn_label))
        self.turns.append(
            Turn(index=idx, query=query, response=response, screenshot_path=shot_path, timestamp=ts)
        )
        return shot_path

    def set_video_path(self, video_path: Path | str) -> None:
        """Record the final path of the converted .mp4 execution video."""
        self.video_path = Path(video_path)

    def generate_pdf(
        self,
        passed: bool,
        judge_verdict: Optional[Any] = None,
        reasoning: str = "",
    ) -> Optional[Path]:
        """Build the evidence PDF report with judging breakdown and screenshots."""
        try:
            pdf_path = self._build_pdf(passed=passed, judge_verdict=judge_verdict, reasoning=reasoning)
            self._write_result_json(passed=passed, judge_verdict=judge_verdict, reasoning=reasoning)
            self._update_latest_pointer()
            return pdf_path
        except Exception as exc:
            print(f"[WARN] Evidence PDF generation failed for {self.scenario_name}: {exc}")
            import traceback
            traceback.print_exc()
            return None

    def _update_latest_pointer(self) -> None:
        """Update 'latest' symlink under reports/<scenario_name>/."""
        latest = self.out_dir.parent / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(self.out_dir.name, target_is_directory=True)
        except OSError:
            pass

    def _write_result_json(
        self,
        passed: bool,
        judge_verdict: Optional[Any],
        reasoning: str,
    ) -> None:
        """Save machine-readable result.json with full evaluation and conversation data."""
        finished_at = datetime.now()
        overall_status = "PASS" if passed else "FAIL"
        criteria_list = []
        if judge_verdict:
            if hasattr(judge_verdict, "overall_status"):
                overall_status = judge_verdict.overall_status
            elif isinstance(judge_verdict, dict):
                overall_status = judge_verdict.get("overall_status", overall_status)

            if hasattr(judge_verdict, "criteria_results"):
                for c in judge_verdict.criteria_results:
                    criteria_list.append({
                        "criterion": getattr(c, "criterion", str(c)),
                        "status": getattr(c, "status", "PASS"),
                        "reason": getattr(c, "reason", ""),
                    })
            elif isinstance(judge_verdict, dict) and "criteria" in judge_verdict:
                criteria_list = judge_verdict["criteria"]

        payload = {
            "scenario": self.scenario_name,
            "case": self.case_label,
            "persona": self.persona,
            "scenario_objective": self.scenario_objective,
            "started": _fmt(self.run_started_at),
            "finished": _fmt(finished_at),
            "overall_status": overall_status,
            "passed": passed,
            "judge_reasoning": reasoning or (getattr(judge_verdict, "summary_reasoning", "") if judge_verdict else ""),
            "criteria_results": criteria_list,
            "video_file": self.video_path.name if self.video_path and self.video_path.exists() else None,
            "video_path": str(self.video_path) if self.video_path else None,
            "turns": [
                {
                    "index": t.index,
                    "timestamp": _fmt(t.timestamp),
                    "query": t.query,
                    "response": t.response,
                    "screenshot": t.screenshot_path.name,
                }
                for t in self.turns
            ],
        }
        (self.out_dir / "result.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    def _build_pdf(
        self,
        passed: bool,
        judge_verdict: Optional[Any],
        reasoning: str,
    ) -> Path:
        finished_at = datetime.now()
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)

        # Determine overall judging status
        status = "PASS" if passed else "FAIL"
        if judge_verdict:
            if hasattr(judge_verdict, "overall_status"):
                status = judge_verdict.overall_status
            elif isinstance(judge_verdict, dict):
                status = judge_verdict.get("overall_status", status)

        colors = _STATUS_COLORS.get(status, _STATUS_COLORS["PASS"])

        # ── Page 1: Overview & Judging Breakdown ──────────────────────────────
        pdf.add_page()

        # Title
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(25, 40, 65)
        pdf.cell(0, 10, _safe("Argos 'Ask Trevor' Evaluation Report"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # Meta info
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(40, 40, 40)
        pdf.cell(35, 7, "Scenario:", new_x="RIGHT", new_y="TOP")
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 7, _safe(f"{self.case_label} ({self.scenario_name})"), new_x="LMARGIN", new_y="NEXT")

        if self.persona:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(35, 7, "Persona:", new_x="RIGHT", new_y="TOP")
            pdf.set_font("Helvetica", "", 11)
            pdf.cell(0, 7, _safe(self.persona), new_x="LMARGIN", new_y="NEXT")

        if self.scenario_objective:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(35, 7, "Objective:", new_x="RIGHT", new_y="TOP")
            pdf.set_font("Helvetica", "", 11)
            pdf.multi_cell(0, 6, _safe(self.scenario_objective), new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(35, 7, "Execution:", new_x="RIGHT", new_y="TOP")
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 7, _safe(f"{_fmt(self.run_started_at)} -> {_fmt(finished_at)}"), new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(35, 7, "Total Turns:", new_x="RIGHT", new_y="TOP")
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 7, str(len(self.turns)), new_x="LMARGIN", new_y="NEXT")

        if self.video_path and self.video_path.exists():
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(35, 7, "Execution Video:", new_x="RIGHT", new_y="TOP")
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(30, 90, 180)
            pdf.cell(0, 7, _safe(self.video_path.name), new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(40, 40, 40)

        pdf.ln(4)

        # ── Overall Verdict Badge ─────────────────────────────────────────────
        pdf.set_fill_color(*colors["bg"])
        pdf.set_draw_color(*colors["border"])
        pdf.set_text_color(*colors["text"])
        pdf.set_font("Helvetica", "B", 14)
        badge_text = f"  OVERALL VERDICT: {status}  "
        pdf.cell(0, 12, _safe(badge_text), border=1, fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(30, 30, 30)
        pdf.ln(5)

        # ── Judging Criteria Breakdown ────────────────────────────────────────
        criteria_list = []
        if judge_verdict:
            if hasattr(judge_verdict, "criteria_results"):
                for c in judge_verdict.criteria_results:
                    criteria_list.append({
                        "criterion": getattr(c, "criterion", str(c)),
                        "status": getattr(c, "status", "PASS"),
                        "reason": getattr(c, "reason", ""),
                    })
            elif isinstance(judge_verdict, dict) and "criteria" in judge_verdict:
                criteria_list = judge_verdict["criteria"]

        if criteria_list:
            pdf.set_font("Helvetica", "B", 12)
            pdf.cell(0, 8, "Judging & Criteria Evaluation:", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

            for item in criteria_list:
                crit_text = item.get("criterion", "")
                c_status = item.get("status", "PASS").upper()
                c_reason = item.get("reason", "")
                c_color = _STATUS_COLORS.get(c_status, _STATUS_COLORS["PASS"])

                pdf.set_font("Helvetica", "B", 10)
                pdf.set_fill_color(*c_color["bg"])
                pdf.set_draw_color(*c_color["border"])
                pdf.set_text_color(*c_color["text"])
                pdf.cell(24, 6, f"[{c_status}]", border=1, fill=True, align="C", new_x="RIGHT", new_y="TOP")

                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(20, 20, 20)
                pdf.cell(4, 6, "", new_x="RIGHT", new_y="TOP")
                pdf.multi_cell(0, 6, _safe(crit_text), new_x="LMARGIN", new_y="NEXT")

                if c_reason:
                    pdf.set_font("Helvetica", "I", 9)
                    pdf.set_text_color(80, 80, 80)
                    pdf.set_left_margin(35)
                    pdf.multi_cell(0, 5, _safe(f"Reasoning: {c_reason}"), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_left_margin(15)

                pdf.ln(2)

        elif reasoning:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, "Judge Reasoning:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(0, 5, _safe(reasoning), new_x="LMARGIN", new_y="NEXT")

        # ── Turn-by-Turn Evidence Pages (Query, Response, Screenshot) ─────────
        for turn in self.turns:
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(20, 40, 70)
            pdf.cell(0, 9, _safe(f"Turn {turn.index}"), new_x="LMARGIN", new_y="NEXT")

            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(100, 100, 100)
            pdf.cell(0, 5, _safe(f"Timestamp: {_fmt(turn.timestamp)}"), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

            # Query Box
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_fill_color(*_NEUTRAL_LABEL_FILL)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(0, 6, "Customer Query:", new_x="LMARGIN", new_y="NEXT", fill=True)

            pdf.set_font("Helvetica", "", 10)
            pdf.set_fill_color(*_QUERY_HIGHLIGHT_FILL)
            pdf.set_draw_color(*_QUERY_HIGHLIGHT_BORDER)
            pdf.multi_cell(
                0, 6, _safe(turn.query) or "(none)",
                new_x="LMARGIN", new_y="NEXT", border=1, fill=True,
            )
            pdf.ln(2)

            # Response Box
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_fill_color(*_NEUTRAL_LABEL_FILL)
            pdf.cell(0, 6, "Trevor AI Response:", new_x="LMARGIN", new_y="NEXT", fill=True)

            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_fill_color(*_RESPONSE_HIGHLIGHT_FILL)
            pdf.set_draw_color(*_RESPONSE_HIGHLIGHT_BORDER)
            pdf.multi_cell(
                0, 5.5, _safe(turn.response) or "(no response captured)",
                new_x="LMARGIN", new_y="NEXT", border=1, fill=True,
            )
            pdf.ln(3)

            # Stamped Screenshot
            if turn.screenshot_path and turn.screenshot_path.exists():
                try:
                    pdf.set_font("Helvetica", "B", 10)
                    pdf.cell(0, 6, "Execution Screenshot:", new_x="LMARGIN", new_y="NEXT")
                    # Calculate fit within remaining page space
                    pdf.image(str(turn.screenshot_path), w=180)
                except Exception as img_err:
                    print(f"[EvidenceRecorder] Image embedding error: {img_err}")

        out_path = self.out_dir / "evidence_report.pdf"
        pdf.output(str(out_path))
        return out_path
