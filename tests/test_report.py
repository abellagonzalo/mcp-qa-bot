import json
import shutil

import pytest
from logfake import end, entry, start

from qa_bot import evidence, report, session


def make_dir(tmp_path, log: bytes, execs: list[dict], polls: list[dict] | None = None):
    (tmp_path / session.LOG_NAME).write_bytes(log)
    doc = {"session": "t", "execs": execs}
    if polls is not None:
        doc["polls"] = polls
    (tmp_path / session.INDEX_NAME).write_text(json.dumps(doc))
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
    assert "### 🤖 #2 · " in md and "· exit 0 · 312 ms" in md
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
    assert "### 🤖 #2 · " in md  # headings stay text
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


def test_rejected_item_is_not_treated_as_a_markerless_exec(qa_dir):
    # {"lines": "1"} has no entry id; it must not match the index record with "id": None.
    bad = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [{"lines": "1"}, 2]}]}
    model = _model(qa_dir, bad)
    assert [c.entry.id for c in model.tests[0].citations] == [2]
    assert model.warnings == [
        'T1: evidence item {"lines": "1"} is not an entry id, {"entry": id, "lines": "a-b"} or {"poll": n}.'
    ]


def test_cited_missing_exec(tmp_path):
    d = make_dir(tmp_path, entry(1, "kubectl get pods", b"ok\r\n"), [
        {"id": 1, "command": "kubectl get pods"},
        {"id": 2, "command": "curl lost", "typed_at": "2026-11-13T14:02:10+00:00"},
    ])
    v = {"tests": [{"id": "T1", "goal": "g", "expected": "e", "verdict": "pass", "evidence": [1, 2]}]}
    model = _model(d, v)
    assert model.tests[0].citations[1] == report.MissingCitation(2)
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
    "### 🤖 #2–#5 · " + report._time(1010.0) + " · polled 3× over 41 s until /PROCESSED/ (matched)",
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


def test_attempt_with_line_range_is_not_folded(poll_dir):
    path, _ = report.render_report(poll_dir, _poll_verdicts([{"entry": 3, "lines": "1"}]))
    md = path.read_text()
    assert "polled" not in md and "### 🤖 #3 · " in md


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
    assert "### 🤖 #1 · " in md and "polled 1× over 0 s" in md and md.count("[attempt") == 1


def test_poll_index_is_covered_by_the_hash(poll_dir):
    doc = json.loads((poll_dir / session.INDEX_NAME).read_text())
    doc["polls"][0]["outcome"] = "exhausted"
    (poll_dir / session.INDEX_NAME).write_text(json.dumps(doc))
    with pytest.raises(session.SessionError, match="changed after"):
        report.render_report(poll_dir, _poll_verdicts([1]))
