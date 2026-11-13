"""The closed session record: owns the session folder's on-disk format.

A session folder holds:

- ``session.log``: the raw pane stream, OSC markers included (see ``transcript``),
- ``session.index.json``: what ``exec``/``poll`` typed (``{"session", "execs", "polls"}``),
- ``session.log.sha256``: hashes of both, written at close.

``write`` is called once by ``Session.close``. ``load`` verifies the hashes and returns
a ``Record``: the log's entries already classified as agent/manual/mismatch, stray
markers, exec calls that left no markers, and poll groups, all as typed values.
Classification happens only inside ``Record.build``, so no caller can read an
unclassified entry. Nothing outside this module reads the folder's files or the index
JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import hooks, transcript
from .transcript import Entry, StrayMarker

LOG_NAME = "session.log"
INDEX_NAME = "session.index.json"
HASH_NAME = "session.log.sha256"


class RecordError(RuntimeError):
    """The session folder is incomplete or changed after the session was closed."""


@dataclass(frozen=True)
class MissingExec:
    """An ``exec`` call (or poll attempt) that left no markers in the log."""

    id: int | None  # None: no start marker was ever seen for it
    command: str
    typed_at: datetime | None


@dataclass(frozen=True)
class PollGroup:
    """A ``poll`` call: its attempts are entry ids in the log."""

    poll_id: int
    command: str
    until: str
    outcome: str
    attempts: tuple[int, ...]


@dataclass(frozen=True)
class Record:
    """A verified, classified closed session."""

    entries: list[Entry]  # every entry has source agent | manual | mismatch
    stray: list[StrayMarker]
    missing: list[MissingExec]
    polls: list[PollGroup]
    log_hash: str = ""

    def by_id(self) -> dict[int, Entry]:
        return {e.id: e for e in self.entries}

    @classmethod
    def build(cls, log: bytes, execs: list[dict], polls: list[dict] | None = None, log_hash: str = "") -> "Record":
        """Parse the log and classify it against the index records (in memory, no files)."""
        t = transcript.parse(log)
        missing = _classify(t.entries, execs)
        return cls(t.entries, t.stray, missing, _poll_groups(polls), log_hash)


# ------------------------------------------------------------------ folder I/O


def write(directory: Path, session: str, execs: list[dict], polls: list[dict]) -> str:
    """Write the index and the hash file next to an existing ``session.log``.

    Returns the log's sha256.
    """
    doc = {"session": session, "execs": execs, "polls": polls}
    (directory / INDEX_NAME).write_text(json.dumps(doc, indent=2) + "\n")
    log_hash = _sha256(directory / LOG_NAME)
    index_hash = _sha256(directory / INDEX_NAME)
    (directory / HASH_NAME).write_text(f"{log_hash}  {LOG_NAME}\n{index_hash}  {INDEX_NAME}\n")
    return log_hash


def verify(directory: Path) -> str:
    """Return the log's sha256 if session.log and the index still match the hash file."""
    hash_file = directory / HASH_NAME
    if not hash_file.exists():
        raise RecordError(f"{hash_file} not found: close the session before rendering the report.")
    expected: dict[str, str] = {}
    for line in hash_file.read_text().splitlines():
        digest, _, name = line.partition("  ")
        expected[name.strip()] = digest.strip()
    for name in (LOG_NAME, INDEX_NAME):
        path = directory / name
        if not path.exists():
            raise RecordError(f"Integrity check failed: {name} is missing.")
        if _sha256(path) != expected.get(name):
            raise RecordError(f"Integrity check failed: {name} changed after the session was closed.")
    return expected[LOG_NAME]


def load(directory: Path) -> Record:
    """Verify the folder's hashes, then parse and classify the closed session."""
    log_hash = verify(directory)
    try:
        doc = json.loads((directory / INDEX_NAME).read_text())
    except json.JSONDecodeError as exc:
        raise RecordError(f"{INDEX_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RecordError(f"{INDEX_NAME} must be a JSON object.")
    execs = doc.get("execs")
    execs = [r for r in execs if isinstance(r, dict)] if isinstance(execs, list) else []
    return Record.build((directory / LOG_NAME).read_bytes(), execs, doc.get("polls"), log_hash)


# -------------------------------------------------------------------- internal


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _classify(entries: list[Entry], execs: list[dict]) -> list[MissingExec]:
    """Mark entries as agent/manual/mismatch using the exec index; return the missing execs."""
    by_id = {e.id: e for e in entries}
    matched: set[int] = set()
    missing: list[MissingExec] = []
    for rec in execs:
        eid = rec.get("id")
        command = _text(rec.get("command"))
        entry = by_id.get(eid) if _is_int(eid) else None
        if entry is None:
            missing.append(MissingExec(eid if _is_int(eid) else None, command, _timestamp(rec.get("typed_at"))))
            continue
        matched.add(entry.id)
        if hooks.normalise_command(entry.command) == hooks.normalise_command(command):
            entry.source = "agent"
        else:
            entry.source = "mismatch"
            entry.flags.append("command in the log differs from the command exec typed")
    for entry in entries:
        if entry.id not in matched:
            entry.source = "manual"
    return missing


def _poll_groups(raw) -> list[PollGroup]:
    """Poll groups from the index (absent in sessions recorded before ``poll`` existed)."""
    groups: list[PollGroup] = []
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict) or not _is_int(g.get("poll_id")):
            continue
        attempts = tuple(a for a in g.get("attempts") or [] if _is_int(a))
        groups.append(
            PollGroup(g["poll_id"], _text(g.get("command")), _text(g.get("until")), _text(g.get("outcome")), attempts)
        )
    return groups
