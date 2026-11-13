"""Preferred tools: which command to use for which job.

Guidance for the agent, not enforcement (``exec`` runs any executable that passes the
exec policy in ``policy.py``). Two Markdown files are merged:

- system:  ``$XDG_CONFIG_HOME/qa-bot/tools.md`` (default ``~/.config/qa-bot/tools.md``)
- project: the nearest ``.qa-bot/tools.md`` in the session folder or a parent folder

Each ``## heading`` is one job (e.g. ``## HTTP to in-cluster services``). A project
section replaces the system section with the same heading (case-insensitive); other
project sections are added after the system ones. Text before the first ``##`` is kept
from both files, system first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = "tools.md"
PROJECT_DIR = ".qa-bot"
_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")


@dataclass
class PreferredTools:
    text: str  # merged Markdown; empty when nothing is configured
    sources: list[Path]

    def describe(self) -> str:
        """The block returned to the agent."""
        if not self.text:
            return (
                "Preferred tools: none configured. Add a system list at "
                f"{system_path()} or a project list at {PROJECT_DIR}/{FILE_NAME}."
            )
        files = ", ".join(str(p) for p in self.sources)
        return f"Preferred tools (guidance, not enforced; from {files}):\n\n{self.text}"


def system_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "qa-bot" / FILE_NAME


def project_path(directory: str | Path | None) -> Path | None:
    """Nearest ``.qa-bot/tools.md`` at or above ``directory`` (which may not exist yet)."""
    if directory is None:
        return None
    start = Path(directory).expanduser().resolve()
    system = system_path().resolve() if system_path().exists() else None
    for folder in (start, *start.parents):
        candidate = folder / PROJECT_DIR / FILE_NAME
        if candidate.is_file() and candidate.resolve() != system:
            return candidate
    return None


def _sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split into (preamble, [(heading, full section text)])."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            sections.append((m.group(1), [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble).strip(), [(h, "\n".join(body).strip()) for h, body in sections]


def merge(system: str, project: str) -> str:
    sys_pre, sys_secs = _sections(system)
    proj_pre, proj_secs = _sections(project)
    merged: dict[str, str] = {h.casefold(): body for h, body in sys_secs}
    for heading, body in proj_secs:
        merged[heading.casefold()] = body  # replaces in place, or appends
    parts = [p for p in (sys_pre, proj_pre) if p] + [b for b in merged.values() if b]
    return "\n\n".join(parts)


def load(directory: str | Path | None = None) -> PreferredTools:
    sources: list[Path] = []
    texts: list[str] = []
    for path in (system_path(), project_path(directory)):
        if path is not None and path.is_file():
            sources.append(path)
            texts.append(path.read_text())
        else:
            texts.append("")
    return PreferredTools(merge(*texts), sources)
