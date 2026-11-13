"""Validate ``verdicts.json`` against the closed log and render ``qa-report.md``.

Everything above the ``###`` evidence blocks comes from the verdicts the agent wrote;
the evidence blocks are copied verbatim from the cleaned log entries, as text, as images
drawn from the same lines (``evidence.py``), or both. The agent never writes evidence
itself.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import evidence
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


@dataclass(frozen=True)
class Citation:
    """Cites lines of an entry that is in the log."""

    entry: transcript.Entry
    lines: tuple[int, int] | None  # 1-based inclusive range, None = whole entry


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
    citations: list[Citation | MissingCitation]

    @property
    def evidence(self) -> list[Citation]:
        return [c for c in self.citations if isinstance(c, Citation)]


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


def _fence(lines: list[str]) -> str:
    longest = max((len(m) for line in lines for m in re.findall(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------- validation


def _parse_citation(
    item, t: transcript.Transcript, missing_ids: set[int], test_id: str, warnings: list[str]
) -> Citation | MissingCitation | None:
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
            return MissingCitation(eid)
        if any(s.id == str(eid) for s in t.stray):
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


def parse_verdicts(verdicts: dict, t: transcript.Transcript, missing: list[dict]) -> Verdicts:
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
    missing_ids = {rec["id"] for rec in missing if isinstance(rec.get("id"), int)}
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
        cits = [c for item in items if (c := _parse_citation(item, t, missing_ids, tid, warnings))]
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
            if c.lines is None and len(c.entry.lines) > limit:
                warnings.append(
                    f"{tid}: #{c.entry.id} has {len(c.entry.lines)} lines; cited whole it is cut at "
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


# ----------------------------------------------------------------- rendering


def _evidence_body(c: Citation) -> tuple[list[str], list[str], int]:
    """Heading parts, ``$ command`` + shown output lines (+ truncation note), output line count."""
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
    return head, body, len(shown)


def _evidence_block(c: Citation, mode: str = "text", image: Path | None = None) -> list[str]:
    """One evidence block: heading, flags, then the cited lines as text, image or both.

    Text and image are drawn from the same ``body`` lines, so they cannot disagree.
    """
    head, body, _ = _evidence_body(c)
    out = [f"### {' · '.join(head)}"]
    for flag in c.entry.flags:
        out.append(f"> ⚠️ {flag}")
    if mode in ("image", "both"):
        if image is None:
            raise ReportError("image evidence needs an image path.")
        evidence.render_png(body, image)
        alt = f"#{c.entry.id}" + (f" lines {c.lines[0]}-{c.lines[1]}" if c.lines else "")
        out += [f"![{alt}]({evidence.EVIDENCE_DIR}/{image.name})", ""]
    fence = _fence(body)
    if mode == "text":
        out += [fence, *body, fence]
    elif mode == "both":
        out += ["<details><summary>text</summary>", "", fence, *body, fence, "", "</details>"]
    out.append("")
    return out


def render(
    verdicts: dict,
    t: transcript.Transcript,
    missing: list[dict],
    log_hash: str,
    mode: str = "text",
    directory: Path | None = None,
) -> tuple[str, list[str]]:
    """Render the report markdown. ``mode`` is text, image or both; image modes write
    PNGs into ``<directory>/evidence/``."""
    if mode not in evidence.MODES:
        raise ReportError(f"evidence must be one of {', '.join(evidence.MODES)}, not '{mode}'.")
    images = mode in ("image", "both")
    if images and directory is None:
        raise ReportError("image evidence needs the session folder.")
    model = parse_verdicts(verdicts, t, missing)
    warnings = model.warnings
    if images:
        img_limit = evidence.max_image_lines()
        for test in model.tests:
            for c in test.evidence:
                shown = _evidence_body(c)[2]
                if shown > img_limit:
                    warnings.append(
                        f"{test.id}: #{c.entry.id} shows {shown} lines; an image over {img_limit} lines "
                        "is hard to read. Cite a narrower line range."
                    )

    cited_by: dict[int, list[str]] = {}
    missing_cited: dict[int, list[str]] = {}
    for test in model.tests:
        for c in test.citations:
            key, table = (c.entry.id, cited_by) if isinstance(c, Citation) else (c.exec_id, missing_cited)
            if test.id not in table.setdefault(key, []):
                table[key].append(test.id)

    title = model.ticket or "QA session"
    meta = [f"{label}: {value}" for label, value in (("Env", model.env), ("Build", model.build)) if value]
    meta.append(f"Log: {sess.LOG_NAME} (sha256 {_short_hash(log_hash)}, {len(t.entries)} entries)")

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

    image_dir = evidence.reset_dir(directory) if images else None
    used_names: set[str] = set()
    for test in model.tests:
        cits = test.evidence
        out.append(f"## {test.id}: {test.goal}")
        fields = [
            ("Input", test.input),
            ("Expected", test.expected),
            ("Actual", test.actual),
            ("Verdict", VERDICT_ICONS.get(test.verdict, "❔ " + test.verdict)),
            ("Note", test.note),
        ]
        out += [f"**{label}:** {value}  " for label, value in fields if value]
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
        for k, c in enumerate(cits, 1):
            image = None
            if images:
                # Duplicate test ids (already warned about) must not overwrite each other's images.
                name, n = evidence.file_name(test.id, k), 1
                while name in used_names:
                    n += 1
                    name = evidence.file_name(f"{test.id}_{n}", k)
                used_names.add(name)
                image = image_dir / name
            out += _evidence_block(c, mode, image)

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
        f"verify with `shasum -a 256 -c {sess.HASH_NAME}`."
        + (
            f" Images in {evidence.EVIDENCE_DIR}/ are rendered from the same lines and can be "
            "regenerated with render_report."
            if images
            else ""
        )
        + "</sub>",
        "",
    ]
    return "\n".join(out), warnings


def render_report(
    directory: Path, verdicts: dict | None = None, mode: str = "text"
) -> tuple[Path, list[str]]:
    """Integrity check, validation and rendering; writes ``<directory>/qa-report.md``.

    ``mode`` is how cited lines appear: ``text`` (code blocks), ``image`` (PNGs in
    ``evidence/``) or ``both``.
    """
    if mode not in evidence.MODES:
        raise ReportError(f"evidence must be one of {', '.join(evidence.MODES)}, not '{mode}'.")
    if mode != "text":
        evidence.freeze_path()  # fail before writing anything
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
    markdown, warnings = render(verdicts, t, missing, log_hash, mode, directory)
    report_path = directory / REPORT_NAME
    report_path.write_text(markdown)
    return report_path, warnings
