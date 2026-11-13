import json

import pytest
from logfake import end, entry, start

from qa_bot import report, session


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
