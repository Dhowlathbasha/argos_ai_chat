"""
utils/video_transcoder.py — Transcode Playwright WebM recordings to MP4 format.
Uses imageio_ffmpeg's bundled ffmpeg binary to ensure cross-platform compatibility
without requiring system-level ffmpeg installation.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def get_ffmpeg_binary() -> Optional[str]:
    """Retrieve path to a functional ffmpeg binary."""
    # 1. Check imageio_ffmpeg bundled binary
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except ImportError:
        pass

    # 2. Check system PATH
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg

    return None


def transcode_webm_to_mp4(
    webm_path: Path | str,
    output_mp4_path: Optional[Path | str] = None,
    remove_source: bool = False,
) -> Optional[Path]:
    """
    Transcode a WebM video file to MP4 (H.264 / AAC).

    Args:
        webm_path: Path to the source .webm file.
        output_mp4_path: Target path for the .mp4 file. If None, replaces extension with .mp4.
        remove_source: Whether to delete the original .webm file after successful transcoding.

    Returns:
        Path to the output .mp4 file if successful, None otherwise.
    """
    src = Path(webm_path)
    if not src.exists() or src.stat().st_size == 0:
        print(f"[VideoTranscoder] Source file not found or empty: {src}")
        return None

    if output_mp4_path is None:
        dst = src.with_suffix(".mp4")
    else:
        dst = Path(output_mp4_path)

    dst.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = get_ffmpeg_binary()
    if not ffmpeg_bin:
        print("[VideoTranscoder] WARNING: No ffmpeg binary available. Cannot convert to MP4.")
        return None

    # ffmpeg command for fast and compatible H.264 encoding
    cmd = [
        ffmpeg_bin,
        "-y",               # Overwrite destination if exists
        "-i", str(src),      # Input file
        "-c:v", "libx264",  # H.264 video codec
        "-pix_fmt", "yuv420p", # Broad player compatibility (QuickTime, Safari, etc.)
        "-preset", "fast",
        "-crf", "23",
        "-movflags", "+faststart", # Web streaming optimization
        str(dst),
    ]

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
        if dst.exists() and dst.stat().st_size > 0:
            if remove_source and src.exists():
                src.unlink()
            return dst
        else:
            print(f"[VideoTranscoder] Transcoding finished but output is missing/empty: {dst}")
            return None
    except subprocess.CalledProcessError as err:
        stderr_msg = err.stderr.decode("utf-8", errors="replace") if err.stderr else ""
        print(f"[VideoTranscoder] ffmpeg error (code {err.returncode}): {stderr_msg[:300]}")
        return None
    except Exception as exc:
        print(f"[VideoTranscoder] Unexpected error transcoding video: {exc}")
        return None
