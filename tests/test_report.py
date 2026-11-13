import json
import shutil
from pathlib import Path

import pytest
from logfake import end, entry, start

from qa_bot import evidence, record, report


def make_dir(tmp_path, log: bytes, execs: list[dict], polls: list[dict] | None = None):
    """A closed session folder, written the way Session.close writes it."""
    (tmp_path / record.LOG_NAME).write_bytes(log)
    record.write(tmp_path, "t", execs, polls or [])
    return tmp_path


POST = "curl -s -o /dev/null -w '%{http_code}' -X POST https://payments.staging.internal/v1/transfers"


@pytest.fixture
def qa_dir(tmp_path):
    log = (
        entry(1, "kubectl config current-context", b"staging\r\n", t=1000.0)
        + entry(2, POST, b"201", t=1001.0)
        + entry(3, "kubectl logs payments-7d9f", b"log a\r\nlog b\r\n", t=1002.0)
        + entry(4, POST, b"201", t=1003.0)
        + entry(5, "psql -c 'select count(*) from transfers'", b" count\r\n-------\r\n     2\r\n(1 row)\r\n", t=1004.0)
        + entry(6, "echo `x`", b"" + start(99, "fake") + end(99), t=1005.0)
    )
    execs = [
        {"id": 1, "command": "kubectl config current-context"},
        {"id": 2, "command": POST},
        {"id": 4, "command": POST},
        {"id": 5, "command": "psql -c 'select count(*) from transfers'"},
        {"id": None, "command": "curl lost"},
    ]
    return make_dir(tmp_path, log, execs)


VERDICTS = {
    "ticket": "PAY-123",
    "env": "staging",
    "build": "payments@a1b2c3d",
    "tests": [
        {
            "id": "T1",
            "goal": "Duplicate idempotency key is rejected",
            "input": "inputs/t1.json",
            "expected": "1st POST 201, 2nd POST 409, one row in transfers",
            "verdict": "fail",
            "actual": "2nd POST returned 201; two rows created",
            "evidence": [2, 4, {"entry": 5, "lines": "1-3"}],
            "note": "IdempotencyFilter is not registered",
        }
    ],
}


ANOMALIES = [
    "Log anomaly: start marker #99 for `fake` (forged: inside another entry's output), found inside the output of #6.",
    "Log anomaly: end marker #99 (forged: inside another entry's output), found inside the output of #6.",
    "Log anomaly: exec (no id) `curl lost` typed at unknown time has no markers in the log (missing).",
    "Manual entries no verdict cites: #3 (`kubectl logs payments-7d9f`), #6 (echo `x`).",
]


@pytest.fixture
def clean_dir(tmp_path):
    log = entry(1, "kubectl config current-context", b"staging\r\n", t=1000.0) + entry(2, POST, b"201", t=1001.0)
    return make_dir(tmp_path, log, [{"id": 1, "command": "kubectl config current-context"}, {"id": 2, "command": POST}])


CLEAN_VERDICTS = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1, 2]}]}


def test_clean_session_has_no_warning_box(clean_dir):
    path, warnings = report.render_report(clean_dir, CLEAN_VERDICTS)
    assert warnings == []
    md = path.read_text()
    assert "[!WARNING]" not in md and "Timeline" not in md


def test_render_report(qa_dir):
    path, warnings = report.render_report(qa_dir, VERDICTS)
    assert warnings == ANOMALIES
    md = path.read_text()
    assert md.startswith("# QA report: PAY-123\nEnv: staging · Build: payments@a1b2c3d · Log: session.log (sha256 ")
    assert "6 entries)" in md
    assert "| T1 | Duplicate idempotency key is rejected | ❌ fail |" in md
    assert "👤 Manual entries during this test: #3 (`kubectl logs payments-7d9f`)" in md
    assert "### E1 · 🤖 #2 · " in md and "· exit 0 · 312 ms" in md
    assert f"$ {POST}\n201\n" in md
    assert "· lines 1-3\n```\n$ psql -c 'select count(*) from transfers'\n count\n-------\n     2\n```" in md
    assert "## Timeline" not in md
    assert "> [!WARNING]\n> Validation warnings:\n" + "".join(f"> - {w}\n" for w in ANOMALIES) in md
    assert "`echo `x``" not in md  # backtick commands are not wrapped in code
    assert json.loads((qa_dir / "verdicts.json").read_text()) == VERDICTS


def test_validation_warnings(qa_dir):
    bad = {
        "tests": [
            {"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [42, {"entry": 2, "lines": "1-9"}]},
            {"id": "T2", "goal": "g", "expected": "e", "verdict": "fail", "evidence": []},
            {"id": "T3", "goal": "g", "verdict": "skipped"},
            {"id": "T4", "goal": "g", "expected": "e", "verdict": "maybe", "evidence": [99]},
        ]
    }
    _, warnings = report.render_report(qa_dir, bad)
    text = "\n".join(warnings)
    assert "T1: cites #42, which is not in the log." in text
    assert "T1: #2 line range 1-9 is outside the entry (1-1)." in text
    assert "T2: cites no log entries." in text
    assert "T2: a failed test needs 'actual' (or 'checks')." in text
    assert "T3" not in text
    assert "T4: verdict 'maybe'" in text
    assert "T4: cites #99, which only exists as a forged/orphan marker." in text


def test_tampered_log_is_rejected(qa_dir):
    with (qa_dir / record.LOG_NAME).open("ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(record.RecordError, match="Integrity"):
        report.render_report(qa_dir, VERDICTS)


def test_long_entry_cited_whole_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_BOT_REPORT_MAX_LINES", "5")
    out = b"".join(b"line %d\r\n" % i for i in range(1, 21))
    d = make_dir(tmp_path, entry(1, "kubectl logs x", out), [{"id": 1, "command": "kubectl logs x"}])
    path, warnings = report.render_report(
        d, {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1]}]}
    )
    assert any("Cite a line range" in w for w in warnings)
    md = path.read_text()
    assert "line 5\n" in md and "line 6\n" not in md
    assert "15 more lines not shown" in md


# ------------------------------------------------------------------ image evidence


class FakeDrawer:
    """Stands in for freeze: records the lines each image is drawn from, writes a stub PNG.

    ``fail_on`` names an image whose drawing fails, to exercise partial failures.
    """

    def __init__(self, fail_on: str | None = None, available: bool = True):
        self.drawn: dict[str, list[str]] = {}
        self.fail_on = fail_on
        self.available = available

    def check(self):
        if not self.available:
            raise evidence.EvidenceError("no drawer")

    def draw(self, lines, dest):
        if dest.name == self.fail_on:
            raise evidence.EvidenceError(f"draw failed for {dest.name}")
        self.drawn[dest.name] = list(lines)
        dest.write_bytes(b"\x89PNG stub")


def test_contents_links_each_test_one_level_only(clean_dir):
    path, _ = report.render_report(clean_dir, CLEAN_VERDICTS)
    md = path.read_text()
    contents = md.split("## Contents\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert contents == ["- [T1: g](#t1) ✅ pass"]
    assert '<a id="t1"></a>' in md
    assert md.index("## Contents") < md.index("## T1: g")


def test_verdict_is_in_the_test_heading(clean_dir):
    md = report.render_report(clean_dir, CLEAN_VERDICTS)[0].read_text()
    assert "## T1: g — ✅ pass\n" in md
    assert "**Verdict:**" not in md


CHECKS = [
    {"check": "HTTP status", "expected": "`200`", "actual": "`200`", "ok": True, "evidence": [1]},
    {"check": "`status`", "expected": "`CANCELLED`", "actual": "`VALIDATED`", "ok": False, "evidence": [2]},
]


def test_checks_render_as_numbered_expected_and_actual_lists(clean_dir):
    verdicts = {"tests": [{"id": "T1", "goal": "g", "verdict": "fail", "checks": CHECKS,
                           "note": "n", "evidence": [1, {"entry": 2, "title": "POST returns 201"}]}]}
    path, warnings = report.render_report(clean_dir, verdicts)
    md = path.read_text()
    assert warnings == []
    expected = "**Expected result**\n\n1. HTTP status: `200`\n2. `status`: `CANCELLED`\n"
    actual = (
        "**Actual result**\n\n"
        "1. ✅ HTTP status: `200` ([E1](#t1-1))\n"
        "2. ❌ `status`: `VALIDATED` (expected `CANCELLED`)\n"
        "\n"
        '   <a id="t1-2"></a>**E2: POST returns 201**  \n'
        "   🤖 #2 · "
    )
    assert expected in md and actual in md
    assert md.index(expected) < md.index(actual) < md.index("**Note:** n")
    assert "   ```\n" + f"   $ {POST}\n   201\n   ```\n" in md  # the failed check's proof, nested
    # E1 backs a passing check: it stays in the Evidence section; E2 is shown only inline
    assert "**Evidence**\n\n" in md and "### E1 · 🤖 #1 · " in md and "### E2" not in md


def test_evidence_shared_by_two_failed_checks_is_shown_once(clean_dir):
    checks = [
        {"check": "a", "expected": "x", "actual": "y", "ok": False, "evidence": [1]},
        {"check": "b", "expected": "x", "actual": "y", "ok": False, "evidence": [1, 2]},
    ]
    verdicts = {"tests": [{"id": "T1", "goal": "g", "verdict": "fail", "checks": checks, "evidence": [1, 2]}]}
    md = report.render_report(clean_dir, verdicts)[0].read_text()
    assert md.count('<a id="t1-1"></a>') == 1
    assert "2. ❌ b: y (expected x) ([E1](#t1-1))\n" in md
    assert "**Evidence**" not in md  # every block is inline under a failed check


def test_checks_collapse_whitespace(clean_dir):
    checks = [{"check": "a|b", "expected": "x", "actual": "y\nz", "ok": True, "evidence": [1]}]
    verdicts = {"tests": [{"id": "T1", "goal": "g", "verdict": "pass", "checks": checks, "evidence": [1]}]}
    md = report.render_report(clean_dir, verdicts)[0].read_text()
    assert "1. ✅ a|b: y z ([E1](#t1-1))\n" in md


def test_template_sections_render_in_order(clean_dir):
    verdicts = {"tests": [{
        "id": "T1", "goal": "g", "verdict": "pass", "requirement": "`PB-1`",
        "preconditions": ["Batch `265` is `OPEN`"], "test_data": ["Batch ID: `265`", "User: `qa`"],
        "steps": ["Send the cancel", "Read the row"], "checks": CHECKS[:1], "evidence": [1],
    }]}
    path, warnings = report.render_report(clean_dir, verdicts)
    md = path.read_text()
    assert warnings == []
    parts = [
        "**Requirement:** `PB-1`  \n",
        "**Pre-conditions**\n\n1. Batch `265` is `OPEN`\n",
        "**Test data**\n\n- Batch ID: `265`\n- User: `qa`\n",
        "**Steps**\n\n1. Send the cancel\n2. Read the row\n",
        "**Expected result**",
        "**Actual result**",
        "**Evidence**",
    ]
    assert all(p in md for p in parts)
    assert [md.index(p) for p in parts] == sorted(md.index(p) for p in parts)


@pytest.mark.parametrize("field, value, warning", [
    ("steps", "one step", None),
    ("steps", 3, "T1: 'steps' must be a list of strings."),
    ("preconditions", ["ok", ""], "T1: preconditions item 2 must be a non-empty string."),
])
def test_template_lists_are_validated(clean_dir, field, value, warning):
    verdicts = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", field: value, "evidence": [1]}]}
    _, warnings = report.render_report(clean_dir, verdicts)
    assert warnings == ([warning] if warning else [])


@pytest.mark.parametrize("verdict, checks, warning", [
    ("pass", CHECKS, "T1: verdict is pass but a check failed."),
    ("fail", CHECKS[:1], "T1: verdict is fail but every check passed."),
    ("pass", [{"check": "c", "expected": "e", "actual": "a"}], "T1: check 'c' needs 'ok': true or false."),
    ("pass", [{"check": "c", "expected": "e", "ok": True}], "T1: check 'c' is missing 'actual'."),
    ("pass", [{"expected": "e", "actual": "a", "ok": True}], "T1: check 1 is missing 'check' (what is checked)."),
    ("pass", "nope", "T1: 'checks' must be a list."),
    ("pass", [{"check": "c", "expected": "e", "actual": "a", "ok": True}],
     "T1: check 'c' links no evidence (add \"evidence\": [n])."),
    ("pass", [{"check": "c", "expected": "e", "actual": "a", "ok": True, "evidence": [3]}],
     "T1: check 'c' cites E3, which is not in the test's evidence list."),
    ("pass", [{"check": "c", "expected": "e", "actual": "a", "ok": True, "evidence": "E1"}],
     "T1: check 'c' evidence must be positions in the test's evidence list, like [1, 2]."),
])
def test_checks_are_validated(clean_dir, verdict, checks, warning):
    verdicts = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": verdict,
                           "checks": checks, "evidence": [1]}]}
    _, warnings = report.render_report(clean_dir, verdicts)
    assert warning in warnings


def test_skipped_test_checks_need_no_actual(clean_dir):
    checks = [{"check": "c", "expected": "e"}]
    verdicts = {"tests": [{"id": "T1", "goal": "g", "verdict": "skipped", "checks": checks, "note": "n"}]}
    path, warnings = report.render_report(clean_dir, verdicts)
    assert warnings == []
    md = path.read_text()
    assert "**Expected result**\n\n1. c: e\n" in md
    assert "**Actual result**" not in md


def test_findings_section_comes_before_contents(clean_dir):
    verdicts = dict(CLEAN_VERDICTS, findings=["Reusing a key on another batch returns `204`", "  two\n lines "])
    path, warnings = report.render_report(clean_dir, verdicts)
    md = path.read_text()
    assert warnings == []
    assert "## Findings\n\n- ⚠️ Reusing a key on another batch returns `204`\n- ⚠️ two lines\n" in md
    assert md.index("## Findings") < md.index("## Contents")


def test_no_findings_no_section(clean_dir):
    assert "## Findings" not in report.render_report(clean_dir, CLEAN_VERDICTS)[0].read_text()


def test_bad_findings_are_warned(clean_dir):
    _, warnings = report.render_report(clean_dir, dict(CLEAN_VERDICTS, findings="x"))
    assert "'findings' must be a list of strings." in warnings
    _, warnings = report.render_report(clean_dir, dict(CLEAN_VERDICTS, findings=["ok", 3]))
    assert "finding 2 must be a non-empty string." in warnings


def test_evidence_titles_head_their_blocks(clean_dir):
    verdicts = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [
        {"entry": 1, "title": "Context is staging"},
        {"entry": 2, "lines": "1", "title": "POST returns 201"},
    ]}]}
    path, warnings = report.render_report(clean_dir, verdicts)
    md = path.read_text()
    assert warnings == []
    assert "### E1: Context is staging\n🤖 #1 · " in md
    assert "### E2: POST returns 201\n🤖 #2 · " in md
    assert "](#t1-" not in md


def test_contents_anchors_are_unique_and_link_text_escaped(clean_dir):
    verdicts = {"tests": [
        {"id": "T1", "goal": "a [b]", "expected": "e", "verdict": "pass", "evidence": [1]},
        {"id": "t1", "goal": "c", "expected": "e", "verdict": "pass", "evidence": [2]},
    ]}
    path, _ = report.render_report(clean_dir, verdicts)
    md = path.read_text()
    assert "- [T1: a \\[b\\]](#t1)" in md
    assert "- [t1: c](#t1_2)" in md
    assert '<a id="t1_2"></a>' in md


@pytest.fixture
def drawer():
    return FakeDrawer()


def images(d):
    return sorted(p.name for p in (d / "evidence").iterdir())


def test_image_evidence(qa_dir, drawer):
    path, warnings = report.render_report(qa_dir, VERDICTS, mode="image", drawer=drawer)
    assert warnings == ANOMALIES
    md = path.read_text()
    assert "![#2](evidence/T1-1.png)" in md
    assert "![#4](evidence/T1-2.png)" in md
    assert "![#5 lines 1-3](evidence/T1-3.png)" in md
    assert f"$ {POST}\n201\n" not in md  # no text blocks in image mode
    assert "### E1 · 🤖 #2 · " in md  # headings stay text
    assert "Images in evidence/ are rendered from the same lines" in md
    assert images(qa_dir) == ["T1-1.png", "T1-2.png", "T1-3.png"]
    assert sorted(p.name for p in qa_dir.iterdir() if p.name.startswith(".evidence")) == []
    # The image is drawn from exactly the lines the text block would show.
    assert drawer.drawn["T1-3.png"] == ["$ psql -c 'select count(*) from transfers'", " count", "-------", "     2"]
    assert drawer.drawn["T1-1.png"] == [f"$ {POST}", "201"]


def test_both_evidence(qa_dir, drawer):
    md = report.render_report(qa_dir, VERDICTS, mode="both", drawer=drawer)[0].read_text()
    assert "![#5 lines 1-3](evidence/T1-3.png)\n\n<details><summary>text</summary>\n\n```\n$ psql" in md
    assert md.count("<details>") == 3


def test_text_mode_writes_no_images(qa_dir, drawer):
    md = report.render_report(qa_dir, VERDICTS, drawer=drawer)[0].read_text()
    assert "![" not in md and not (qa_dir / "evidence").exists() and drawer.drawn == {}


def test_rerender_removes_stale_images(qa_dir, drawer):
    report.render_report(qa_dir, VERDICTS, mode="image", drawer=drawer)
    fewer = json.loads(json.dumps(VERDICTS))
    fewer["tests"][0]["evidence"] = [2]
    report.render_report(qa_dir, fewer, mode="image", drawer=drawer)
    assert images(qa_dir) == ["T1-1.png"]


def test_failed_draw_keeps_previous_report_and_images(qa_dir, drawer):
    """A drawer failing partway must not leave the old report linking to deleted images."""
    path, _ = report.render_report(qa_dir, VERDICTS, mode="image", drawer=drawer)
    before_md = path.read_text()
    with pytest.raises(evidence.EvidenceError, match="T1-2.png"):
        report.render_report(qa_dir, VERDICTS, mode="image", drawer=FakeDrawer(fail_on="T1-2.png"))
    assert path.read_text() == before_md
    assert images(qa_dir) == ["T1-1.png", "T1-2.png", "T1-3.png"]
    assert not (qa_dir / evidence.STAGING_DIR).exists()


def test_failed_first_image_render_leaves_no_folder(qa_dir):
    with pytest.raises(evidence.EvidenceError):
        report.render_report(qa_dir, VERDICTS, mode="image", drawer=FakeDrawer(fail_on="T1-1.png"))
    assert not (qa_dir / "evidence").exists() and not (qa_dir / report.REPORT_NAME).exists()
    assert not (qa_dir / evidence.STAGING_DIR).exists()


def test_duplicate_test_ids_do_not_overwrite_images(qa_dir, drawer):
    t = {"goal": "g", "expected": "e", "verdict": "pass"}
    dup = {"tests": [{**t, "id": "T 1/a", "evidence": [2]}, {**t, "id": "T 1/a", "evidence": [4]}]}
    md = report.render_report(qa_dir, dup, mode="image", drawer=drawer)[0].read_text()
    assert "(evidence/T_1_a-1.png)" in md and "(evidence/T_1_a_2-1.png)" in md


def test_tall_image_warns(tmp_path, drawer, monkeypatch):
    monkeypatch.setenv("QA_BOT_IMAGE_MAX_LINES", "3")
    out = b"".join(b"line %d\r\n" % i for i in range(1, 6))
    d = make_dir(tmp_path, entry(1, "kubectl logs x", out), [{"id": 1, "command": "kubectl logs x"}])
    t = {"id": "T1", "goal": "g", "expected": "e", "verdict": "pass"}
    path, warnings = report.render_report(d, {"tests": [{**t, "evidence": [1]}]}, mode="image", drawer=drawer)
    tall = "T1: #1 is 6 lines as an image; over 3 lines is hard to read. Cite a narrower line range."
    assert warnings == [tall]
    assert f"> - {tall}" in path.read_text()  # warnings found while drawing still reach the header
    lines = {"entry": 1, "lines": "2-3"}
    _, warnings = report.render_report(d, {"tests": [{**t, "evidence": [lines]}]}, mode="image", drawer=drawer)
    assert warnings == []


def test_unknown_mode_is_rejected(qa_dir):
    with pytest.raises(evidence.EvidenceError, match="evidence must be one of"):
        report.render_report(qa_dir, VERDICTS, mode="png")
    assert not (qa_dir / "verdicts.json").exists()


def test_unavailable_drawer_fails_before_writing(qa_dir):
    with pytest.raises(evidence.EvidenceError, match="no drawer"):
        report.render_report(qa_dir, VERDICTS, mode="image", drawer=FakeDrawer(available=False))
    assert not (qa_dir / report.REPORT_NAME).exists()
    assert not (qa_dir / "verdicts.json").exists()


def test_freeze_missing_from_path_is_reported(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(evidence.EvidenceError, match="needs freeze"):
        evidence.Freeze().check()


def test_freeze_draws_without_window_at_scale_1_by_default(monkeypatch):
    monkeypatch.delenv("QA_BOT_IMAGE_SCALE", raising=False)
    monkeypatch.setattr(evidence.shutil, "which", lambda name: "/bin/freeze")
    args = evidence.Freeze().args(Path("x.png"))
    assert "--window" not in args
    assert args[args.index("--font.size") + 1] == "3.5"
    assert args[args.index("--padding") + 1] == "5,10,5,5"


def test_image_scale_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("QA_BOT_IMAGE_SCALE", "2")
    assert evidence.Freeze().scale == 2
    monkeypatch.setenv("QA_BOT_IMAGE_SCALE", "nope")
    assert evidence.Freeze().scale == 1


def _png_width(path):
    return int.from_bytes(path.read_bytes()[16:20], "big")


@pytest.mark.skipif(not shutil.which("freeze"), reason="freeze not installed")
def test_real_freeze_honours_scale(tmp_path):
    lines = ["$ echo hi", "hi"]
    evidence.Freeze(scale=1).draw(lines, tmp_path / "one.png")
    evidence.Freeze(scale=2).draw(lines, tmp_path / "two.png")
    one, two = _png_width(tmp_path / "one.png"), _png_width(tmp_path / "two.png")
    assert abs(two - 2 * one) <= 2


@pytest.mark.skipif(not shutil.which("freeze"), reason="freeze not installed")
def test_real_freeze_renders_png(qa_dir):
    report.render_report(qa_dir, VERDICTS, mode="image")
    png = (qa_dir / "evidence" / "T1-3.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# ------------------------------------------------------------------ verdict model


def _model(qa_dir, verdicts):
    return report.parse_verdicts(verdicts, record.load(qa_dir))


def test_render_from_an_in_memory_record():
    rec = record.Record.build(
        entry(1, "kubectl get pods", b"ok\r\n") + entry(2, "ls", b"a\r\n"),
        [{"id": 1, "command": "kubectl get pods"}, {"id": 7, "command": "curl lost", "typed_at": "2026-11-13T09:05:01+01:00"}],
        log_hash="ab" * 32,
    )
    md, warnings = report.render(
        {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1]}]}, rec
    )
    assert "Log: session.log (sha256 abab…abab, 2 entries)" in md
    assert "### E1 · 🤖 #1 · " in md
    assert warnings == [
        "Log anomaly: exec #7 `curl lost` typed at 09:05:01 has no markers in the log (missing).",
        "Manual entries no verdict cites: #2 (`ls`).",
    ]


def test_parse_verdicts_normalizes_tests(qa_dir):
    model = _model(qa_dir, VERDICTS)
    assert model.warnings == []
    assert (model.ticket, model.env, model.build) == ("PAY-123", "staging", "payments@a1b2c3d")
    (t1,) = model.tests
    assert (t1.id, t1.verdict, t1.input) == ("T1", "fail", "inputs/t1.json")
    assert [(c.entry.id, c.lines) for c in t1.evidence] == [(2, None), (4, None), (5, (1, 3))]


def test_parse_verdicts_resolves_missing_id_once(qa_dir):
    model = _model(qa_dir, {"tests": [{"goal": "g", "verdict": "skipped"}, {"id": " ", "goal": "g", "verdict": "skipped"}]})
    assert [t.id for t in model.tests] == ["test 1", "test 2"]
    assert model.warnings == ["test 1: missing 'id'.", "test 2: missing 'id'."]


def test_non_list_evidence_warns_and_renders(qa_dir):
    # Used to crash render with "TypeError: 'int' object is not iterable".
    bad = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": 5}]}
    path, warnings = report.render_report(qa_dir, bad)
    assert "T1: 'evidence' must be a list." in warnings
    assert "T1: cites no log entries." in warnings
    assert "## T1: g" in path.read_text()


def test_rejected_item_is_not_treated_as_a_markerless_exec(qa_dir):
    # {"lines": "1"} has no entry id; it must not match the index record with "id": None.
    bad = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [{"lines": "1"}, 2]}]}
    model = _model(qa_dir, bad)
    assert [c.entry.id for c in model.tests[0].citations] == [2]
    assert model.warnings == [
        'T1: evidence item {"lines": "1"} is not an entry id, {"entry": id, "lines": "a-b", "title": "…"} or {"poll": n, "title": "…"}.'
    ]


def test_cited_missing_exec(tmp_path):
    d = make_dir(tmp_path, entry(1, "kubectl get pods", b"ok\r\n"), [
        {"id": 1, "command": "kubectl get pods"},
        {"id": 2, "command": "curl lost", "typed_at": "2026-11-13T14:02:10+00:00"},
    ])
    v = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1, 2]}]}
    model = _model(d, v)
    assert [c.entry.id for c in model.tests[0].evidence] == [1]
    _, warnings = report.render_report(d, v)
    assert warnings == [
        "T1: cites #2, an exec call with no markers in the log (missing).",
        "Log anomaly: exec #2 `curl lost` typed at 14:02:10 has no markers in the log (missing).",
    ]


def test_manual_entries_cited_by_a_verdict_are_not_warned(qa_dir):
    v = json.loads(json.dumps(VERDICTS))
    v["tests"][0]["evidence"] += [3, 6]
    _, warnings = report.render_report(qa_dir, v)
    assert not any(w.startswith("Manual entries") for w in warnings)
    assert sum(w.startswith("Log anomaly") for w in warnings) == 3  # markers and missing stay


def test_uncited_flagged_entry_warns(tmp_path):
    d = make_dir(tmp_path, entry(1, "kubectl get pods", b"ok\r\n") + entry(2, "printf 1", b"1"), [
        {"id": 1, "command": "kubectl get pods"},
        {"id": 2, "command": "printf 2"},
    ])
    v = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1]}]}
    _, warnings = report.render_report(d, v)
    assert warnings == ["Log anomaly: #2 `printf 1`: command in the log differs from the command exec typed."]


# ------------------------------------------------------------------- poll groups

GET = 'curl -s https://x/v1/batches/276 | jq \'{id, status}\''


@pytest.fixture
def poll_dir(tmp_path):
    log = (
        entry(1, "kubectl config current-context", b"staging\r\n", t=1000.0)
        + entry(2, GET, b'{ "id": 276, "status": "PENDING" }\r\n', t=1010.0)
        + entry(3, GET, b'{ "id": 276, "status": "PENDING" }\r\n', t=1030.0)
        + entry(4, "echo typed by human", b"typed by human\r\n", t=1040.0)
        + entry(5, GET, b'{ "id": 276, "status": "PROCESSED" }\r\n', t=1050.0, dur=1.0)
    )
    execs = [{"id": 1, "command": "kubectl config current-context"}] + [
        {"id": i, "command": GET, "poll_id": 1} for i in (2, 3, 5)
    ]
    polls = [{"poll_id": 1, "command": GET, "until": "PROCESSED", "interval": 20,
              "max_attempts": 10, "attempts": [2, 3, 5], "outcome": "matched"}]
    return make_dir(tmp_path, log, execs, polls)


def _poll_verdicts(evidence):
    return {"tests": [{"id": "T1", "goal": "batch gets processed", "expected": "PROCESSED",
                       "verdict": "pass", "evidence": evidence}]}


POLL_BLOCK = [
    "### E1 · 🤖 #2–#5 · " + report._time(1010.0) + " · polled 3× over 41 s until /PROCESSED/ (matched)",
    "```",
    f"$ {GET}",
    "[attempt 1 · " + report._time(1010.0) + "]",
    '{ "id": 276, "status": "PENDING" }',
    "[attempt 3 · " + report._time(1050.0) + "]",
    '{ "id": 276, "status": "PROCESSED" }',
    "```",
]


@pytest.mark.parametrize("evidence", [[{"poll": 1}], [3], [2, 5], [{"entry": 5}], [{"poll": 1}, 3]])
def test_cited_poll_folds_into_one_block(poll_dir, evidence):
    path, warnings = report.render_report(poll_dir, _poll_verdicts(evidence))
    md = path.read_text()
    assert "\n".join(POLL_BLOCK) in md
    assert md.count("### ") == 1
    # the human's command between attempts is surfaced, as for any test
    assert "👤 Manual entries during this test: #4" in md
    assert warnings == ["Manual entries no verdict cites: #4 (`echo typed by human`)."]


def test_titled_poll_citation(poll_dir):
    path, _ = report.render_report(poll_dir, _poll_verdicts([{"poll": 1, "title": "Batch reaches PROCESSED"}]))
    md = path.read_text()
    assert "### E1: Batch reaches PROCESSED\n" in md


def test_attempt_with_line_range_is_not_folded(poll_dir):
    path, _ = report.render_report(poll_dir, _poll_verdicts([{"entry": 3, "lines": "1"}]))
    md = path.read_text()
    assert "polled" not in md and "### E1 · 🤖 #3 · " in md


def test_unknown_poll_is_warned(poll_dir):
    _, warnings = report.render_report(poll_dir, _poll_verdicts([{"poll": 7}, 1]))
    assert "T1: cites poll P7, which is not in the session index." in warnings


def test_poll_image_evidence(poll_dir, drawer):
    path, _ = report.render_report(poll_dir, _poll_verdicts([5]), "both", drawer=drawer)
    md = path.read_text()
    assert "![poll P1 #2-#5](evidence/T1-1.png)" in md
    assert drawer.drawn["T1-1.png"] == POLL_BLOCK[2:-1]


def test_single_attempt_poll(tmp_path):
    log = entry(1, GET, b'{ "status": "PROCESSED" }\r\n', t=1000.0)
    d = make_dir(tmp_path, log, [{"id": 1, "command": GET, "poll_id": 1}],
                 [{"poll_id": 1, "command": GET, "until": "PROCESSED", "attempts": [1], "outcome": "matched"}])
    md = report.render_report(d, _poll_verdicts([1]))[0].read_text()
    assert "### E1 · 🤖 #1 · " in md and "polled 1× over 0 s" in md and md.count("[attempt") == 1


def test_poll_index_is_covered_by_the_hash(poll_dir):
    doc = json.loads((poll_dir / record.INDEX_NAME).read_text())
    doc["polls"][0]["outcome"] = "exhausted"
    (poll_dir / record.INDEX_NAME).write_text(json.dumps(doc))
    with pytest.raises(record.RecordError, match="changed after"):
        report.render_report(poll_dir, _poll_verdicts([1]))
