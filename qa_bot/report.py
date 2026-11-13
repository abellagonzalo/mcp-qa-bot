"""Validate ``verdicts.json`` against the closed log and render ``qa-report.md``.

Everything above the ``###`` evidence blocks comes from the verdicts the agent wrote;
the evidence blocks are copied verbatim from the cleaned log entries, as text, as images
drawn from the same lines (``evidence.py``), or both. The agent never writes evidence
itself. A cited poll group (``poll`` tool) is one block showing its first and last
attempt.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import evidence, transcript
from .record import HASH_NAME, LOG_NAME, PollGroup, Record
from .record import load as load_record

REPORT_NAME = "qa-report.md"
VERDICTS_NAME = "verdicts.json"
VERDICT_ICONS = {"pass": "✅ pass", "fail": "❌ fail", "skipped": "⏭️ skipped"}
SOURCE_ICONS = {"agent": "🤖", "manual": "👤", "mismatch": "⚠️"}
_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


class ReportError(RuntimeError):
    pass


def max_uncited_lines() -> int:
    try:
        return max(1, int(os.environ.get("QA_BOT_REPORT_MAX_LINES", "200")))
    except ValueError:
        return 200


@dataclass(frozen=True)
class Citation:
    """Cites lines of an entry that is in the log."""

    entry: transcript.Entry
    lines: tuple[int, int] | None  # 1-based inclusive range, None = whole entry


    @property
    def entry_ids(self) -> list[int]:
        return [self.entry.id]


@dataclass(frozen=True)
class PollCitation:
    """Cites a whole poll group; shown as one block (first and last attempt)."""

    group: PollGroup
    entries: tuple[transcript.Entry, ...]  # attempts found in the log, in order

    @property
    def entry_ids(self) -> list[int]:
        return [e.id for e in self.entries]


@dataclass(frozen=True)
class MissingCitation:
    """Cites an ``exec`` call that left no markers in the log (always warned about)."""

    exec_id: int


@dataclass
class TestCase:
    """One verdict, normalized: rendering reads only this, never the raw dict."""

    id: str  # resolved once: the given id, or "test N"
    goal: str
    verdict: str  # lower-cased; may be invalid (warned about)
    input: str
    expected: str
    actual: str
    note: str
    citations: list[Citation | PollCitation | MissingCitation]

    @property
    def evidence(self) -> list[Citation | PollCitation]:
        return [c for c in self.citations if not isinstance(c, MissingCitation)]

    @property
    def cited_ids(self) -> set[int]:
        return {i for c in self.evidence for i in c.entry_ids}


@dataclass
class Verdicts:
    ticket: str
    env: str
    build: str
    tests: list[TestCase]
    warnings: list[str]


# ------------------------------------------------------------------- helpers


def _time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S") if epoch else "—"


def _short_hash(digest: str) -> str:
    return f"{digest[:4]}…{digest[-4:]}"


def _one_line(cmd: str, width: int = 80) -> str:
    cmd = re.sub(r"[ \t]*\\\n[ \t]*", " ", cmd)  # join `\` line continuations
    first, *rest = cmd.split("\n")
    text = first + (" …" if rest else "")
    return text if len(text) <= width else text[: width - 1] + "…"


def _table_code(cmd: str, width: int = 80) -> str:
    text = _one_line(cmd, width).replace("|", "\\|")
    return f"`{text}`" if "`" not in text else text


def _text(value) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------- validation


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _poll_citation(group: PollGroup, rec: Record, test_id: str, warnings: list[str]) -> PollCitation | None:
    by_id = rec.by_id()
    entries = tuple(by_id[a] for a in group.attempts if a in by_id)
    if not entries:
        warnings.append(f"{test_id}: cites poll P{group.poll_id}, which has no attempts in the log.")
        return None
    return PollCitation(group, entries)


def _parse_citation(
    item,
    rec: Record,
    missing_ids: set[int],
    test_id: str,
    warnings: list[str],
) -> Citation | PollCitation | MissingCitation | None:
    by_id = rec.by_id()
    polls = rec.polls
    if isinstance(item, dict) and set(item) == {"poll"} and _is_int(item["poll"]):
        group = next((g for g in polls if g.poll_id == item["poll"]), None)
        if group is None:
            warnings.append(f"{test_id}: cites poll P{item['poll']}, which is not in the session index.")
            return None
        return _poll_citation(group, rec, test_id, warnings)
    if _is_int(item):
        eid, lines = item, None
    elif isinstance(item, dict) and _is_int(item.get("entry")):
        eid, lines = item["entry"], item.get("lines")
    else:
        warnings.append(
            f"{test_id}: evidence item {json.dumps(item)} is not an entry id, "
            f"{{\"entry\": id, \"lines\": \"a-b\"}} or {{\"poll\": n}}."
        )
        return None

    if lines is None and eid in by_id:
        group = next((g for g in polls if eid in g.attempts), None)
        if group is not None:  # an attempt cited whole stands for its poll group
            return _poll_citation(group, rec, test_id, warnings)

    entry = by_id.get(eid)
    if entry is None:
        if eid in missing_ids:
            warnings.append(f"{test_id}: cites #{eid}, an exec call with no markers in the log (missing).")
            return MissingCitation(eid)
        if any(s.id == str(eid) for s in rec.stray):
            warnings.append(f"{test_id}: cites #{eid}, which only exists as a forged/orphan marker.")
        else:
            warnings.append(f"{test_id}: cites #{eid}, which is not in the log.")
        return None

    if lines is None:
        return Citation(entry, None)
    m = _RANGE.match(str(lines))
    if not m:
        warnings.append(f"{test_id}: #{eid} line range '{lines}' is not 'N' or 'A-B'.")
        return Citation(entry, None)
    start = int(m.group(1))
    end = int(m.group(2) or start)
    total = len(entry.lines)
    if start < 1 or end < start or end > total:
        warnings.append(f"{test_id}: #{eid} line range {start}-{end} is outside the entry (1-{total}).")
        return Citation(entry, None)
    return Citation(entry, (start, end))


def parse_verdicts(verdicts: dict, rec: Record) -> Verdicts:
    """Parse the agent's verdicts once into a normalized model, collecting warnings.

    Raises ``ReportError`` only when the shape is unusable (not an object, no test list).
    """
    if not isinstance(verdicts, dict):
        raise ReportError("verdicts must be a JSON object.")
    raw_tests = verdicts.get("tests")
    if not isinstance(raw_tests, list):
        raise ReportError("verdicts.tests must be a list.")

    warnings: list[str] = []
    # Exec calls that never saw a start marker are indexed with "id": None; they can't be cited.
    missing_ids = {m.id for m in rec.missing if m.id is not None}
    seen: set[str] = set()
    tests: list[TestCase] = []
    limit = max_uncited_lines()
    for n, test in enumerate(raw_tests, 1):
        if not isinstance(test, dict):
            raise ReportError(f"verdicts.tests[{n - 1}] must be an object.")
        tid = _text(test.get("id")) or f"test {n}"
        if not _text(test.get("id")):
            warnings.append(f"{tid}: missing 'id'.")
        if tid in seen:
            warnings.append(f"{tid}: duplicate test id.")
        seen.add(tid)
        goal = _text(test.get("goal"))
        if not goal:
            warnings.append(f"{tid}: missing 'goal'.")
        verdict = _text(test.get("verdict")).lower()
        if verdict not in VERDICT_ICONS:
            warnings.append(f"{tid}: verdict '{test.get('verdict')}' must be pass, fail or skipped.")
        expected, actual = _text(test.get("expected")), _text(test.get("actual"))
        if verdict in ("pass", "fail") and not expected:
            warnings.append(f"{tid}: missing 'expected'.")
        if verdict == "fail" and not actual:
            warnings.append(f"{tid}: a failed test needs 'actual'.")

        items = test.get("evidence")
        if items is None:
            items = []
        elif not isinstance(items, list):
            warnings.append(f"{tid}: 'evidence' must be a list.")
            items = []
        cits: list[Citation | PollCitation | MissingCitation] = []
        seen_polls: set[int] = set()
        for item in items:
            c = _parse_citation(item, rec, missing_ids, tid, warnings)
            if isinstance(c, PollCitation):
                if c.group.poll_id in seen_polls:
                    continue  # several attempts of one poll cited: one block
                seen_polls.add(c.group.poll_id)
            if c is not None:
                cits.append(c)
        case = TestCase(
            id=tid,
            goal=goal,
            verdict=verdict,
            input=_text(test.get("input")),
            expected=expected,
            actual=actual,
            note=_text(test.get("note")),
            citations=cits,
        )
        if not case.evidence and verdict != "skipped":
            warnings.append(f"{tid}: cites no log entries.")
        for c in case.evidence:
            shown = [c.entry] if isinstance(c, Citation) and c.lines is None else []
            if isinstance(c, PollCitation):
                shown = _poll_shown(c)
            for e in shown:
                if len(e.lines) > limit:
                    warnings.append(
                        f"{tid}: #{e.id} has {len(e.lines)} lines; cited whole it is cut at "
                        f"{limit} lines. Cite a line range instead."
                    )
        tests.append(case)
    return Verdicts(
        ticket=_text(verdicts.get("ticket")),
        env=_text(verdicts.get("env")),
        build=_text(verdicts.get("build")),
        tests=tests,
        warnings=warnings,
    )


def log_anomalies(rec: Record, tests: list[TestCase]) -> list[str]:
    """Warnings about the log itself; they can't be fixed by editing verdicts.

    Forged or stray markers, ``exec`` calls with no markers, flagged entries no verdict
    cites, and manual entries no verdict cites (they may have changed state).
    """
    cited = {i for test in tests for i in test.cited_ids}
    out: list[str] = []
    for s in rec.stray:
        where = f"inside the output of #{s.inside}" if s.inside is not None else "at top level"
        cmd = f" for {_table_code(s.command, 50)}" if s.command else ""
        out.append(f"Log anomaly: {s.kind} marker #{s.id}{cmd} ({s.reason}), found {where}.")
    for m in rec.missing:
        label = f"#{m.id}" if m.id is not None else "(no id)"
        typed = m.typed_at.strftime("%H:%M:%S") if m.typed_at else "unknown time"
        out.append(
            f"Log anomaly: exec {label} {_table_code(m.command, 50)} typed at {typed} "
            "has no markers in the log (missing)."
        )
    for e in rec.entries:
        if e.flags and e.id not in cited:
            out.append(f"Log anomaly: #{e.id} {_table_code(e.command, 50)}: {'; '.join(e.flags)}.")
    manual = [e for e in rec.entries if e.source == "manual" and e.id not in cited]
    if manual:
        listed = ", ".join(f"#{e.id} ({_table_code(e.command, 50)})" for e in manual)
        out.append(f"Manual entries no verdict cites: {listed}.")
    return out


# ----------------------------------------------------------------- rendering


def _poll_shown(c: PollCitation) -> list[transcript.Entry]:
    """The attempts a poll block shows: the first and the last."""
    return [c.entries[0]] if len(c.entries) == 1 else [c.entries[0], c.entries[-1]]


def _capped(lines: list[str]) -> tuple[list[str], str | None]:
    limit = max_uncited_lines()
    if len(lines) <= limit:
        return lines, None
    return lines[:limit], f"… {len(lines) - limit} more lines not shown (lines 1-{limit} of {len(lines)})"


def _poll_body(c: PollCitation) -> tuple[list[str], list[str]]:
    g = c.group
    first, last = c.entries[0], c.entries[-1]
    ids = f"#{first.id}" if first is last else f"#{first.id}–#{last.id}"
    head = [f"{SOURCE_ICONS.get(first.source, '❔')} {ids}", _time(first.started)]
    end = last.ended or last.started
    elapsed = max(0, round(end - first.started)) if first.started and end else 0
    n = len(g.attempts)
    head.append(f"polled {n}× over {elapsed} s until /{g.until}/ ({g.outcome or 'unknown'})")
    cmd_lines = first.command.split("\n")
    body = [f"$ {cmd_lines[0]}", *cmd_lines[1:]]
    for e in _poll_shown(c):
        attempt = g.attempts.index(e.id) + 1
        label = [f"attempt {attempt}", _time(e.started)]
        if e.exit_code != 0:
            label.append(f"exit {e.exit_code}" if e.finished else "still running")
        body.append(f"[{' · '.join(label)}]")
        shown, note = _capped(e.lines)
        body += shown
        if note:
            body.append(note)
    return head, body


def _evidence_body(c: Citation | PollCitation) -> tuple[list[str], list[str]]:
    """Heading parts, and ``$ command`` + shown output lines (+ truncation note)."""
    if isinstance(c, PollCitation):
        return _poll_body(c)
    e = c.entry
    head = [f"{SOURCE_ICONS.get(e.source, '❔')} #{e.id}", _time(e.started)]
    head.append(f"exit {e.exit_code}" if e.finished else "still running")
    if e.duration_ms is not None:
        head.append(f"{e.duration_ms} ms")
    lines = e.lines
    note = None
    if c.lines:
        a, b = c.lines
        head.append(f"lines {a}-{b}" if a != b else f"line {a}")
        shown = lines[a - 1 : b]
    else:
        shown, note = _capped(lines)
    cmd_lines = e.command.split("\n")
    body = [f"$ {cmd_lines[0]}", *cmd_lines[1:], *shown]
    if note:
        body.append(note)
    return head, body


def _flags(c: Citation | PollCitation) -> list[str]:
    if isinstance(c, Citation):
        return list(c.entry.flags)
    return [f"#{e.id}: {flag}" for e in c.entries for flag in e.flags]


def _alt(c: Citation | PollCitation) -> str:
    if isinstance(c, PollCitation):
        return f"poll P{c.group.poll_id} #{c.entries[0].id}-#{c.entries[-1].id}"
    return f"#{c.entry.id}" + (f" lines {c.lines[0]}-{c.lines[1]}" if c.lines else "")


def _evidence_block(c: Citation | PollCitation, blocks: evidence.Renderer, test_id: str, k: int) -> list[str]:
    """One evidence block: heading and flags, then the cited lines as ``blocks`` renders them."""
    head, body = _evidence_body(c)
    out = [f"### {' · '.join(head)}"]
    out += [f"> ⚠️ {flag}" for flag in _flags(c)]
    out += blocks.block(test_id, k, _alt(c), body)
    out.append("")
    return out


def render(
    verdicts: dict,
    rec: Record,
    blocks: evidence.Renderer | None = None,
) -> tuple[str, list[str]]:
    """Render the report markdown for a loaded record. ``blocks`` renders the evidence (default: text)."""
    blocks = blocks or evidence.Renderer()
    model = parse_verdicts(verdicts, rec)

    sections: list[str] = []
    for test in model.tests:
        cits = test.evidence
        sections.append(f"## {test.id}: {test.goal}")
        fields = [
            ("Input", test.input),
            ("Expected", test.expected),
            ("Actual", test.actual),
            ("Verdict", VERDICT_ICONS.get(test.verdict, "❔ " + test.verdict)),
            ("Note", test.note),
        ]
        sections += [f"**{label}:** {value}  " for label, value in fields if value]
        if cits:
            cited = test.cited_ids
            ids = sorted(cited)
            manual = [
                e for e in rec.entries
                if e.source == "manual" and min(ids) < e.id < max(ids) and e.id not in cited
            ]
            if manual:
                listed = ", ".join(f"#{e.id} ({_table_code(e.command, 50)})" for e in manual)
                sections.append(f"👤 Manual entries during this test: {listed}  ")
        sections.append("")
        for k, c in enumerate(cits, 1):
            sections += _evidence_block(c, blocks, test.id, k)

    warnings = model.warnings + blocks.warnings + log_anomalies(rec, model.tests)

    title = model.ticket or "QA session"
    meta = [f"{label}: {value}" for label, value in (("Env", model.env), ("Build", model.build)) if value]
    meta.append(f"Log: {LOG_NAME} (sha256 {_short_hash(rec.log_hash)}, {len(rec.entries)} entries)")

    out: list[str] = [f"# QA report: {title}", " · ".join(meta), ""]

    if warnings:
        out += ["> [!WARNING]", "> Validation warnings:"]
        out += [f"> - {w}" for w in warnings]
        out.append("")

    out += ["| Test | Goal | Verdict |", "|---|---|---|"]
    for test in model.tests:
        icon = VERDICT_ICONS.get(test.verdict, "❔ " + test.verdict)
        goal = test.goal.replace("|", "\\|")
        out.append(f"| {test.id} | {goal} | {icon} |")
    out.append("")
    out += sections

    out += [
        f"<sub>Rendered by mcp-qa-bot. Evidence blocks are copied verbatim from {LOG_NAME}; "
        f"verify with `shasum -a 256 -c {HASH_NAME}`.{blocks.footer()}</sub>",
        "",
    ]
    return "\n".join(out), warnings


def render_report(
    directory: Path,
    verdicts: dict | None = None,
    mode: str = "text",
    drawer: evidence.Drawer | None = None,
) -> tuple[Path, list[str]]:
    """Integrity check, validation and rendering; writes ``<directory>/qa-report.md``.

    ``mode`` is how cited lines appear: ``text`` (code blocks), ``image`` (PNGs in
    ``evidence/``) or ``both``. ``drawer`` draws the images (default: ``freeze``).
    If rendering fails, the previous report and its images are left untouched.
    """
    directory = Path(directory).expanduser().resolve()
    blocks = evidence.Renderer(mode, directory, drawer)  # validates mode, preflights the drawer
    rec = load_record(directory)  # verifies the hashes first

    verdicts_path = directory / VERDICTS_NAME
    if verdicts is not None:
        verdicts_path.write_text(json.dumps(verdicts, indent=2, ensure_ascii=False) + "\n")
    elif verdicts_path.exists():
        try:
            verdicts = json.loads(verdicts_path.read_text())
        except json.JSONDecodeError as exc:
            raise ReportError(f"{verdicts_path} is not valid JSON: {exc}") from exc
    else:
        raise ReportError(f"No verdicts given and {verdicts_path} does not exist.")

    with blocks:
        markdown, warnings = render(verdicts, rec, blocks)
    report_path = directory / REPORT_NAME
    report_path.write_text(markdown)
    return report_path, warnings
