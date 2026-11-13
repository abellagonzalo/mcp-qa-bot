"""Validate ``verdicts.json`` against the closed log and render ``qa-report.md``.

Everything above the ``###`` evidence blocks comes from the verdicts the agent wrote;
the evidence blocks are copied verbatim from the cleaned log entries. The agent never
writes evidence itself.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import session as sess
from . import transcript

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


@dataclass
class Citation:
    entry: transcript.Entry
    lines: tuple[int, int] | None  # 1-based inclusive range, None = whole entry


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


def _fence(lines: list[str]) -> str:
    longest = max((len(m) for line in lines for m in re.findall(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------- validation


def _parse_citation(
    item, t: transcript.Transcript, missing_ids: set, test_id: str, warnings: list[str]
) -> Citation | None:
    by_id = t.by_id()
    if isinstance(item, bool):
        item = None
    if isinstance(item, int):
        eid, lines = item, None
    elif isinstance(item, dict) and isinstance(item.get("entry"), int) and not isinstance(item.get("entry"), bool):
        eid, lines = item["entry"], item.get("lines")
    else:
        warnings.append(f"{test_id}: evidence item {json.dumps(item)} is not an entry id or {{\"entry\": id, \"lines\": \"a-b\"}}.")
        return None

    entry = by_id.get(eid)
    if entry is None:
        if eid in missing_ids:
            warnings.append(f"{test_id}: cites #{eid}, an exec call with no markers in the log (missing).")
        elif any(s.id == str(eid) for s in t.stray):
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


def validate(verdicts: dict, t: transcript.Transcript, missing: list[dict]) -> tuple[list[dict], list[list[Citation]], list[str]]:
    if not isinstance(verdicts, dict):
        raise ReportError("verdicts must be a JSON object.")
    tests = verdicts.get("tests")
    if not isinstance(tests, list):
        raise ReportError("verdicts.tests must be a list.")

    warnings: list[str] = []
    missing_ids = {rec.get("id") for rec in missing}
    seen: set[str] = set()
    citations: list[list[Citation]] = []
    for n, test in enumerate(tests, 1):
        if not isinstance(test, dict):
            raise ReportError(f"verdicts.tests[{n - 1}] must be an object.")
        tid = _text(test.get("id")) or f"test {n}"
        if not _text(test.get("id")):
            warnings.append(f"{tid}: missing 'id'.")
        if tid in seen:
            warnings.append(f"{tid}: duplicate test id.")
        seen.add(tid)
        if not _text(test.get("goal")):
            warnings.append(f"{tid}: missing 'goal'.")
        verdict = _text(test.get("verdict")).lower()
        if verdict not in VERDICT_ICONS:
            warnings.append(f"{tid}: verdict '{test.get('verdict')}' must be pass, fail or skipped.")
        if verdict in ("pass", "fail") and not _text(test.get("expected")):
            warnings.append(f"{tid}: missing 'expected'.")
        if verdict == "fail" and not _text(test.get("actual")):
            warnings.append(f"{tid}: a failed test needs 'actual'.")

        evidence = test.get("evidence") or []
        if not isinstance(evidence, list):
            warnings.append(f"{tid}: 'evidence' must be a list.")
            evidence = []
        cits = [c for item in evidence if (c := _parse_citation(item, t, missing_ids, tid, warnings))]
        if not cits and verdict != "skipped":
            warnings.append(f"{tid}: cites no log entries.")
        limit = max_uncited_lines()
        for c in cits:
            if c.lines is None and len(c.entry.lines) > limit:
                warnings.append(
                    f"{tid}: #{c.entry.id} has {len(c.entry.lines)} lines; cited whole it is cut at "
                    f"{limit} lines. Cite a line range instead."
                )
        citations.append(cits)
    return tests, citations, warnings


# ----------------------------------------------------------------- rendering


def _evidence_block(c: Citation) -> list[str]:
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
        limit = max_uncited_lines()
        shown = lines[:limit]
        if len(lines) > limit:
            note = f"… {len(lines) - limit} more lines not shown (lines 1-{limit} of {len(lines)})"
    cmd_lines = e.command.split("\n")
    body = [f"$ {cmd_lines[0]}", *cmd_lines[1:], *shown]
    if note:
        body.append(note)
    fence = _fence(body)
    out = [f"### {' · '.join(head)}"]
    for flag in e.flags:
        out.append(f"> ⚠️ {flag}")
    out += [fence, *body, fence, ""]
    return out


def render(
    verdicts: dict,
    t: transcript.Transcript,
    missing: list[dict],
    log_hash: str,
) -> tuple[str, list[str]]:
    tests, citations, warnings = validate(verdicts, t, missing)

    cited_by: dict[int, list[str]] = {}
    for test, cits in zip(tests, citations):
        tid = _text(test.get("id")) or "?"
        for c in cits:
            if tid not in cited_by.setdefault(c.entry.id, []):
                cited_by[c.entry.id].append(tid)
    missing_cited: dict[int, list[str]] = {}
    for test in tests:
        for item in test.get("evidence") or []:
            eid = item.get("entry") if isinstance(item, dict) else item
            if any(rec.get("id") == eid for rec in missing):
                missing_cited.setdefault(eid, []).append(_text(test.get("id")))

    title = _text(verdicts.get("ticket")) or "QA session"
    meta = []
    for key, label in (("env", "Env"), ("build", "Build")):
        if _text(verdicts.get(key)):
            meta.append(f"{label}: {_text(verdicts.get(key))}")
    meta.append(f"Log: {sess.LOG_NAME} (sha256 {_short_hash(log_hash)}, {len(t.entries)} entries)")

    out: list[str] = [f"# QA report: {title}", " · ".join(meta), ""]

    if warnings:
        out += ["> [!WARNING]", "> Validation warnings:"]
        out += [f"> - {w}" for w in warnings]
        out.append("")

    out += ["| Test | Goal | Verdict |", "|---|---|---|"]
    for test in tests:
        verdict = _text(test.get("verdict")).lower()
        goal = _text(test.get("goal")).replace("|", "\\|")
        icon = VERDICT_ICONS.get(verdict, "❔ " + verdict)
        out.append(f"| {_text(test.get('id'))} | {goal} | {icon} |")
    out.append("")

    by_id = t.by_id()
    for test, cits in zip(tests, citations):
        tid = _text(test.get("id"))
        verdict = _text(test.get("verdict")).lower()
        out.append(f"## {tid}: {_text(test.get('goal'))}")
        fields = [
            ("Input", test.get("input")),
            ("Expected", test.get("expected")),
            ("Actual", test.get("actual")),
            ("Verdict", VERDICT_ICONS.get(verdict, "❔ " + verdict)),
            ("Note", test.get("note")),
        ]
        out += [f"**{label}:** {_text(value)}  " for label, value in fields if _text(value)]
        if cits:
            ids = [c.entry.id for c in cits]
            cited = set(ids)
            manual = [
                e for e in t.entries
                if e.source == "manual" and min(ids) < e.id < max(ids) and e.id not in cited
            ]
            if manual:
                listed = ", ".join(f"#{e.id} ({_table_code(e.command, 50)})" for e in manual)
                out.append(f"👤 Manual entries during this test: {listed}  ")
        out.append("")
        for c in cits:
            out += _evidence_block(c)

    out += [
        "## Timeline",
        "| # | Time | Source | Command | Exit | Cited by |",
        "|---|---|---|---|---|---|",
    ]
    for e in sorted(t.entries, key=lambda e: e.start_offset):
        source = SOURCE_ICONS.get(e.source, "❔")
        if e.flags:
            source += " ⚠️"
        exit_code = str(e.exit_code) if e.finished else "running"
        cites = ", ".join(cited_by.get(e.id, [])) or "—"
        out.append(f"| {e.id} | {_time(e.started)} | {source} | {_table_code(e.command)} | {exit_code} | {cites} |")
    for s in t.stray:
        where = f"inside #{s.inside}" if s.inside is not None else "top level"
        cmd = _table_code(s.command) if s.command else "—"
        out.append(f"| {s.id} | — | ⚠️ {s.kind} marker ({s.reason}, {where}) | {cmd} | — | — |")
    for rec in missing:
        typed = _text(rec.get("typed_at"))[11:19] or "—"
        eid = rec.get("id")
        cites = ", ".join(missing_cited.get(eid, [])) or "—"
        out.append(
            f"| {eid if eid is not None else '—'} | {typed} | 🤖 ⚠️ missing (no markers in the log) | "
            f"{_table_code(_text(rec.get('command')))} | — | {cites} |"
        )
    out += [
        "",
        f"<sub>Rendered by mcp-qa-bot. Evidence blocks are copied verbatim from {sess.LOG_NAME}; "
        f"verify with `shasum -a 256 -c {sess.HASH_NAME}`.</sub>",
        "",
    ]
    return "\n".join(out), warnings


def render_report(directory: Path, verdicts: dict | None = None) -> tuple[Path, list[str]]:
    """Integrity check, validation and rendering; writes ``<directory>/qa-report.md``."""
    directory = Path(directory).expanduser().resolve()
    log_hash = sess.verify_hash_file(directory)

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

    index = json.loads((directory / sess.INDEX_NAME).read_text()).get("execs", [])
    t = transcript.parse((directory / sess.LOG_NAME).read_bytes())
    missing = transcript.classify(t, index)
    markdown, warnings = render(verdicts, t, missing, log_hash)
    report_path = directory / REPORT_NAME
    report_path.write_text(markdown)
    return report_path, warnings
