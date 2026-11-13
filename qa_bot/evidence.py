"""Turn cited log lines into report evidence blocks: text, image or both.

``report`` hands over the lines of one citation (``$ command`` followed by output) and
gets back markdown. Everything about images lives here: mode validation, the drawer
preflight, file naming, size warnings, link paths and the text/image/both layout.

Images are a derivative of ``session.log``, drawn by
`freeze <https://github.com/charmbracelet/freeze>`_. They are not hashed; anyone can
regenerate them from the hashed log with render_report. A render draws into a staging
folder and only replaces ``evidence/`` once every image was drawn, so a failed render
never leaves an existing report pointing at deleted images.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

EVIDENCE_DIR = "evidence"
STAGING_DIR = ".evidence.new"
OLD_DIR = ".evidence.old"
MODES = ("text", "image", "both")
WRAP = 120


class EvidenceError(RuntimeError):
    pass


def max_image_lines() -> int:
    try:
        return max(1, int(os.environ.get("QA_BOT_IMAGE_MAX_LINES", "60")))
    except ValueError:
        return 60


class Drawer(Protocol):
    """Draws lines as a PNG. The only seam: the real ``freeze`` or a test fake."""

    def check(self) -> None:
        """Raise ``EvidenceError`` if drawing can't work (called before anything is written)."""

    def draw(self, lines: list[str], dest: Path) -> None:
        """Write ``lines`` as a PNG to ``dest``; raise ``EvidenceError`` on failure."""


class Freeze:
    """Draws with the ``freeze`` binary."""

    def _path(self) -> str:
        path = shutil.which("freeze")
        if not path:
            raise EvidenceError(
                "Image evidence needs freeze (brew install charmbracelet/tap/freeze); "
                "render with evidence='text' or install it."
            )
        return path

    def check(self) -> None:
        self._path()

    def draw(self, lines: list[str], dest: Path) -> None:
        result = subprocess.run(
            [self._path(), "--language", "text", "--window", "--wrap", str(WRAP), "-o", str(dest)],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not dest.exists():
            raise EvidenceError(f"freeze failed for {dest.name}: {(result.stderr or result.stdout).strip()}")


def file_name(test_id: str, k: int) -> str:
    """``<test id>-<k>.png`` with the test id made safe for a file name."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", test_id).strip("._") or "test"
    return f"{safe}-{k}.png"


def _fence(lines: list[str]) -> str:
    longest = max((len(m) for line in lines for m in re.findall(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


class Renderer:
    """Renders the evidence blocks of one report.

    Use as a context manager: images are drawn into a staging folder and swapped into
    ``<directory>/evidence/`` on a clean exit; on an exception the staging folder is
    discarded and the previous ``evidence/`` is left as it was. Text mode touches no files.
    """

    def __init__(self, mode: str = "text", directory: Path | None = None, drawer: Drawer | None = None):
        if mode not in MODES:
            raise EvidenceError(f"evidence must be one of {', '.join(MODES)}, not '{mode}'.")
        self.mode = mode
        self.images = mode in ("image", "both")
        self.warnings: list[str] = []
        self._used: set[str] = set()
        self._drawer = drawer or Freeze()
        self._dir = Path(directory) if directory is not None else None
        if self.images:
            if self._dir is None:
                raise EvidenceError("image evidence needs the session folder.")
            self._drawer.check()  # fail before anything is written
        self._staging: Path | None = None

    # -- lifecycle

    def __enter__(self) -> Renderer:
        if self.images:
            self._staging = self._dir / STAGING_DIR
            shutil.rmtree(self._staging, ignore_errors=True)  # left by a crashed render
            self._staging.mkdir()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._staging is None:
            return
        if exc_type is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            return
        target, old = self._dir / EVIDENCE_DIR, self._dir / OLD_DIR
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        self._staging.rename(target)
        shutil.rmtree(old, ignore_errors=True)

    # -- blocks

    def _name(self, test_id: str, k: int) -> str:
        # Duplicate test ids (warned about by report) must not overwrite each other's images.
        name, n = file_name(test_id, k), 1
        while name in self._used:
            n += 1
            name = file_name(f"{test_id}_{n}", k)
        self._used.add(name)
        return name

    def block(self, test_id: str, k: int, alt: str, body: list[str]) -> list[str]:
        """Markdown for the ``k``-th citation of ``test_id``: ``body`` as text, image or both.

        Text and image are drawn from the same ``body`` lines, so they cannot disagree.
        """
        out: list[str] = []
        if self.images:
            if self._staging is None:
                raise EvidenceError("image blocks must be rendered inside 'with Renderer(...)'.")
            limit = max_image_lines()
            if len(body) > limit:
                self.warnings.append(
                    f"{test_id}: {alt} is {len(body)} lines as an image; over {limit} lines "
                    "is hard to read. Cite a narrower line range."
                )
            name = self._name(test_id, k)
            self._drawer.draw(body, self._staging / name)
            out += [f"![{alt}]({EVIDENCE_DIR}/{name})", ""]
        fence = _fence(body)
        if self.mode == "text":
            out += [fence, *body, fence]
        elif self.mode == "both":
            out += ["<details><summary>text</summary>", "", fence, *body, fence, "", "</details>"]
        return out

    def footer(self) -> str:
        """Sentence for the report footer, or ``""`` in text mode."""
        if not self.images:
            return ""
        return (
            f" Images in {EVIDENCE_DIR}/ are rendered from the same lines and can be "
            "regenerated with render_report."
        )
