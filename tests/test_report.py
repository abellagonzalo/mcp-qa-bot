import json
import shutil

import pytest
from logfake import end, entry, start

from qa_bot import evidence, report, session


def make_dir(tmp_path, log: bytes, execs: list[dict]):
    (tmp_path / session.LOG_NAME).write_bytes(log)
    (tmp_path / session.INDEX_NAME).write_text(json.dumps({"session": "t", "execs": execs}))
    session.write_hash_file(tmp_path)
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


def test_render_report(qa_dir):
    path, warnings = report.render_report(qa_dir, VERDICTS)
    assert warnings == []
    md = path.read_text()
    assert md.startswith("# QA report: PAY-123\nEnv: staging · Build: payments@a1b2c3d · Log: session.log (sha256 ")
    assert "6 entries)" in md
    assert "| T1 | Duplicate idempotency key is rejected | ❌ fail |" in md
    assert "👤 Manual entries during this test: #3 (`kubectl logs payments-7d9f`)" in md
    assert "### 🤖 #2 · " in md and "· exit 0 · 312 ms" in md
    assert f"$ {POST}\n201\n" in md
    assert "· lines 1-3\n```\n$ psql -c 'select count(*) from transfers'\n count\n-------\n     2\n```" in md
    assert "| 3 |" in md and "| 👤 |" in md
    assert "⚠️ start marker (forged: inside another entry's output, inside #6)" in md
    assert "missing (no markers in the log)" in md
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
    assert "T2: a failed test needs 'actual'." in text
    assert "T3" not in text
    assert "T4: verdict 'maybe'" in text
    assert "T4: cites #99, which only exists as a forged/orphan marker." in text


def test_tampered_log_is_rejected(qa_dir):
    with (qa_dir / session.LOG_NAME).open("ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(session.SessionError, match="Integrity"):
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


@pytest.fixture
def fake_freeze(monkeypatch):
    """Replace freeze: record the lines each image is drawn from, write a stub PNG."""
    drawn: dict[str, list[str]] = {}

    def render_png(lines, dest):
        drawn[dest.name] = list(lines)
        dest.write_bytes(b"\x89PNG stub")

    monkeypatch.setattr(evidence, "freeze_path", lambda: "/usr/bin/true")
    monkeypatch.setattr(evidence, "render_png", render_png)
    return drawn


def test_image_evidence(qa_dir, fake_freeze):
    path, warnings = report.render_report(qa_dir, VERDICTS, mode="image")
    assert warnings == []
    md = path.read_text()
    assert "![#2](evidence/T1-1.png)" in md
    assert "![#4](evidence/T1-2.png)" in md
    assert "![#5 lines 1-3](evidence/T1-3.png)" in md
    assert f"$ {POST}\n201\n" not in md  # no text blocks in image mode
    assert "### 🤖 #2 · " in md  # headings stay text
    assert "Images in evidence/ are rendered from the same lines" in md
    assert sorted(p.name for p in (qa_dir / "evidence").iterdir()) == ["T1-1.png", "T1-2.png", "T1-3.png"]
    # The image is drawn from exactly the lines the text block would show.
    assert fake_freeze["T1-3.png"] == ["$ psql -c 'select count(*) from transfers'", " count", "-------", "     2"]
    assert fake_freeze["T1-1.png"] == [f"$ {POST}", "201"]


def test_both_evidence(qa_dir, fake_freeze):
    md = report.render_report(qa_dir, VERDICTS, mode="both")[0].read_text()
    assert "![#5 lines 1-3](evidence/T1-3.png)\n\n<details><summary>text</summary>\n\n```\n$ psql" in md
    assert md.count("<details>") == 3


def test_text_mode_writes_no_images(qa_dir, fake_freeze):
    md = report.render_report(qa_dir, VERDICTS)[0].read_text()
    assert "![" not in md and not (qa_dir / "evidence").exists() and fake_freeze == {}


def test_rerender_removes_stale_images(qa_dir, fake_freeze):
    report.render_report(qa_dir, VERDICTS, mode="image")
    fewer = json.loads(json.dumps(VERDICTS))
    fewer["tests"][0]["evidence"] = [2]
    report.render_report(qa_dir, fewer, mode="image")
    assert [p.name for p in (qa_dir / "evidence").iterdir()] == ["T1-1.png"]


def test_duplicate_test_ids_do_not_overwrite_images(qa_dir, fake_freeze):
    t = {"goal": "g", "expected": "e", "verdict": "pass"}
    dup = {"tests": [{**t, "id": "T 1/a", "evidence": [2]}, {**t, "id": "T 1/a", "evidence": [4]}]}
    md = report.render_report(qa_dir, dup, mode="image")[0].read_text()
    assert "(evidence/T_1_a-1.png)" in md and "(evidence/T_1_a_2-1.png)" in md


def test_tall_image_warns(tmp_path, fake_freeze, monkeypatch):
    monkeypatch.setenv("QA_BOT_IMAGE_MAX_LINES", "3")
    out = b"".join(b"line %d\r\n" % i for i in range(1, 6))
    d = make_dir(tmp_path, entry(1, "kubectl logs x", out), [{"id": 1, "command": "kubectl logs x"}])
    t = {"id": "T1", "goal": "g", "expected": "e", "verdict": "pass"}
    _, warnings = report.render_report(d, {"tests": [{**t, "evidence": [1]}]}, mode="image")
    assert any("T1: #1 shows 5 lines" in w for w in warnings)
    _, warnings = report.render_report(d, {"tests": [{**t, "evidence": [{"entry": 1, "lines": "2-3"}]}]}, mode="image")
    assert warnings == []


def test_unknown_mode_is_rejected(qa_dir):
    with pytest.raises(report.ReportError, match="evidence must be one of"):
        report.render_report(qa_dir, VERDICTS, mode="png")


def test_missing_freeze_fails_before_writing(qa_dir, monkeypatch):
    monkeypatch.setattr(evidence.shutil, "which", lambda _: None)
    with pytest.raises(evidence.EvidenceError, match="freeze"):
        report.render_report(qa_dir, VERDICTS, mode="image")
    assert not (qa_dir / report.REPORT_NAME).exists()
    assert not (qa_dir / "verdicts.json").exists()


@pytest.mark.skipif(not shutil.which("freeze"), reason="freeze not installed")
def test_real_freeze_renders_png(qa_dir):
    report.render_report(qa_dir, VERDICTS, mode="image")
    png = (qa_dir / "evidence" / "T1-3.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# ------------------------------------------------------------------ verdict model


def _model(qa_dir, verdicts):
    from qa_bot import transcript

    index = json.loads((qa_dir / session.INDEX_NAME).read_text())["execs"]
    t = transcript.parse((qa_dir / session.LOG_NAME).read_bytes())
    missing = transcript.classify(t, index)
    return report.parse_verdicts(verdicts, t, missing)


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


def test_rejected_item_is_not_credited_to_a_markerless_exec(qa_dir):
    # {"lines": "1"} has no entry id; it must not match the index record with "id": None.
    bad = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [{"lines": "1"}, 2]}]}
    model = _model(qa_dir, bad)
    assert [c.entry.id for c in model.tests[0].citations] == [2]
    md = report.render_report(qa_dir, bad)[0].read_text()
    (row,) = [line for line in md.splitlines() if "missing (no markers in the log)" in line]
    assert row.endswith("| — | — |")


def test_cited_missing_exec_is_credited_in_timeline(tmp_path):
    d = make_dir(tmp_path, entry(1, "kubectl get pods", b"ok\r\n"), [
        {"id": 1, "command": "kubectl get pods"},
        {"id": 2, "command": "curl lost"},
    ])
    v = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1, 2]}]}
    model = _model(d, v)
    assert model.tests[0].citations[1] == report.MissingCitation(2)
    assert [c.entry.id for c in model.tests[0].evidence] == [1]
    assert "T1: cites #2, an exec call with no markers in the log (missing)." in model.warnings
    md = report.render_report(d, v)[0].read_text()
    assert "| 2 | — | 🤖 ⚠️ missing (no markers in the log) | `curl lost` | — | T1 |" in md
