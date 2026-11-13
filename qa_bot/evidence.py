"""Render cited log lines as PNG images for the report.

The images are a derivative of ``session.log``: the same ``$ command`` + output lines
the text evidence blocks show, drawn by `freeze <https://github.com/charmbracelet/freeze>`_.
They are not hashed; anyone can regenerate them from the hashed log with render_report.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

EVIDENCE_DIR = "evidence"
MODES = ("text", "image", "both")
WRAP = 120


class EvidenceError(RuntimeError):
    pass


def max_image_lines() -> int:
    try:
        return max(1, int(os.environ.get("QA_BOT_IMAGE_MAX_LINES", "60")))
    except ValueError:
        return 60


def freeze_path() -> str:
    path = shutil.which("freeze")
    if not path:
        raise EvidenceError(
            "Image evidence needs freeze (brew install charmbracelet/tap/freeze); "
            "render with evidence='text' or install it."
        )
    return path


def file_name(test_id: str, k: int) -> str:
    """``<test id>-<k>.png`` with the test id made safe for a file name."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", test_id).strip("._") or "test"
    return f"{safe}-{k}.png"


def reset_dir(directory: Path) -> Path:
    """Empty ``<directory>/evidence`` so no stale image from an earlier render survives."""
    target = directory / EVIDENCE_DIR
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    return target


def render_png(lines: list[str], dest: Path) -> None:
    """Draw ``lines`` (``$ command`` followed by output) as a terminal-style PNG."""
    result = subprocess.run(
        [freeze_path(), "--language", "text", "--window", "--wrap", str(WRAP), "-o", str(dest)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or not dest.exists():
        raise EvidenceError(f"freeze failed for {dest.name}: {(result.stderr or result.stdout).strip()}")
