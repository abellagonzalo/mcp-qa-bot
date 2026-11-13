"""Parse the raw tmux transcript (``session.log``) into entries.

The log is exactly what the pane received, including the OSC markers written by the
zsh hooks. Everything here is deterministic: the same log always yields the same
entries, the same cleaned text and the same line numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MARKER = re.compile(rb"\x1b\]777;qa;(start|end);([^\x07\x1b]*)(?:\x07|\x1b\\)")

# zsh PROMPT_SP: when the output lacks a trailing newline, zsh prints PROMPT_EOL_MARK
# (default: bold reverse '%' or '#'), pads with spaces and returns with "\r \r". It is
# printed before precmd, so it lands inside the entry; strip it.
_PROMPT_SP = re.compile(
    rb"(?:(?:\x1b\[[0-9;]*m)*[%#](?:\x1b\[[0-9;]*m)*)? *\r \r\r?\Z"
)
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_STRING_SEQ = re.compile(r"\x1b[PX^_].*?\x1b\\", re.S)  # DCS, SOS, PM, APC
_ESC_OTHER = re.compile(r"\x1b[ -/]*[0-~]")
_CONTROL = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class Entry:
    id: int
    command: str
    started: float
    start_offset: int  # byte offset right after the start marker
    end_offset: int | None = None  # byte offset of the end marker
    exit_code: int | None = None
    ended: float | None = None
    raw: bytes = b""
    source: str = "manual"  # agent | manual | mismatch; set by record.Record.build
    flags: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.exit_code is not None

    @property
    def duration_ms(self) -> int | None:
        if self.ended is None:
            return None
        return max(0, round((self.ended - self.started) * 1000))

    @property
    def lines(self) -> list[str]:
        return clean_output(self.raw)


@dataclass
class StrayMarker:
    """A marker that is not a valid top-level start/end pair (forged or orphaned)."""

    kind: str  # start | end
    id: str
    command: str
    offset: int
    inside: int | None  # id of the entry whose output contains it, if any
    reason: str


@dataclass
class Transcript:
    entries: list[Entry]
    stray: list[StrayMarker]

    def by_id(self) -> dict[int, Entry]:
        return {e.id: e for e in self.entries}


def _float(text: str) -> float:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return 0.0


def normalise_command(cmd: str) -> str:
    """Command text as it appears in markers: \\r\\n from the tty's onlcr becomes \\n."""
    cmd = cmd.replace("\r\n", "\n").replace("\r", "\n")
    return cmd.replace("\x07", "").replace("\x1b", "").strip()


def parse(log: bytes) -> Transcript:
    entries: list[Entry] = []
    stray: list[StrayMarker] = []
    current: Entry | None = None

    for m in MARKER.finditer(log):
        kind = m.group(1).decode()
        payload = m.group(2).decode("utf-8", errors="replace")
        if kind == "start":
            parts = payload.split(";", 2)
            sid = parts[0]
            cmd = normalise_command(parts[2]) if len(parts) > 2 else ""
            if current is not None:
                stray.append(
                    StrayMarker("start", sid, cmd, m.start(), current.id, "forged: inside another entry's output")
                )
                continue
            if not sid.isdigit():
                stray.append(StrayMarker("start", sid, cmd, m.start(), None, "malformed marker"))
                continue
            current = Entry(
                id=int(sid),
                command=cmd,
                started=_float(parts[1]) if len(parts) > 1 else 0.0,
                start_offset=m.end(),
            )
        else:
            parts = payload.split(";", 2)
            sid = parts[0]
            if current is None or sid != str(current.id):
                stray.append(
                    StrayMarker(
                        "end",
                        sid,
                        "",
                        m.start(),
                        current.id if current else None,
                        "forged: inside another entry's output" if current else "orphan end marker",
                    )
                )
                continue
            current.end_offset = m.start()
            current.raw = log[current.start_offset : m.start()]
            try:
                current.exit_code = int(parts[1])
            except (IndexError, ValueError):
                current.exit_code = None
                current.flags.append("unreadable exit code")
            current.ended = _float(parts[2]) if len(parts) > 2 else None
            entries.append(current)
            current = None

    if current is not None:  # still running when the log ended
        current.raw = log[current.start_offset :]
        current.flags.append("unterminated: no end marker (still running when the log was closed)")
        entries.append(current)

    return Transcript(entries, stray)


def _apply_overwrites(line: str) -> str:
    """Replay \\r and \\b on one line so the text matches what was on screen."""
    buf: list[str] = []
    col = 0
    for ch in line:
        if ch == "\r":
            col = 0
        elif ch == "\b":
            col = max(0, col - 1)
        else:
            if col < len(buf):
                buf[col] = ch
            else:
                buf.extend(" " * (col - len(buf)))
                buf.append(ch)
            col += 1
    return "".join(buf).rstrip()


def clean_output(raw: bytes) -> list[str]:
    """Bytes between an entry's markers -> the lines a human saw, numbered from 1."""
    raw = _PROMPT_SP.sub(b"", raw)
    text = raw.decode("utf-8", errors="replace")
    text = _OSC.sub("", text)
    text = _STRING_SEQ.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC_OTHER.sub("", text)
    text = text.replace("\r\n", "\n")
    text = text.replace("\t", "    ")
    text = _CONTROL.sub("", text)
    lines = [_apply_overwrites(line) for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines

