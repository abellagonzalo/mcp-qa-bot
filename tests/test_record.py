import json
from datetime import datetime, timedelta, timezone

import pytest
from logfake import entry, start

from qa_bot import record


def test_build_classifies_entries_and_types_missing_execs():
    rec = record.Record.build(
        entry(1, "kubectl get ns", b"") + entry(2, "ls", b"") + entry(3, "curl y", b""),
        [
            {"id": 1, "command": "kubectl get ns"},
            {"id": 3, "command": "curl DIFFERENT"},
            {"id": None, "command": "curl z", "typed_at": "2026-11-13T14:02:10.123+01:00"},
            {"id": 9, "command": "curl gone", "typed_at": "not a time"},
        ],
    )
    assert [e.source for e in rec.entries] == ["agent", "manual", "mismatch"]
    assert rec.entries[2].flags == ["command in the log differs from the command exec typed"]
    assert rec.missing == [
        record.MissingExec(None, "curl z", datetime(2026, 11, 13, 14, 2, 10, 123000, timezone(timedelta(hours=1)))),
        record.MissingExec(9, "curl gone", None),
    ]


def test_build_without_index_marks_everything_manual():
    rec = record.Record.build(entry(1, "ls", b"") + start(2, "sleep 9"), [])
    assert [(e.id, e.source) for e in rec.entries] == [(1, "manual"), (2, "manual")]
    assert rec.polls == [] and rec.missing == []


def test_poll_groups_are_typed_and_tolerant():
    rec = record.Record.build(b"", [], [
        {"poll_id": 1, "command": "curl x", "until": "OK", "attempts": [2, "3", True, 4], "outcome": "matched"},
        {"poll_id": "2"},
        "junk",
    ])
    assert rec.polls == [record.PollGroup(1, "curl x", "OK", "matched", (2, 4))]
    assert record.Record.build(b"", [], None).polls == []  # sessions from before poll existed


@pytest.fixture
def closed(tmp_path):
    (tmp_path / record.LOG_NAME).write_bytes(entry(1, "ls", b"a\r\n") + entry(2, "pwd", b"/\r\n"))
    log_hash = record.write(tmp_path, "s", [{"id": 1, "command": "ls"}], [])
    return tmp_path, log_hash


def test_write_then_load_round_trip(closed):
    d, log_hash = closed
    assert json.loads((d / record.INDEX_NAME).read_text()) == {
        "session": "s", "execs": [{"id": 1, "command": "ls"}], "polls": []
    }
    rec = record.load(d)
    assert rec.log_hash == log_hash == record.verify(d)
    assert [(e.id, e.source) for e in rec.entries] == [(1, "agent"), (2, "manual")]


@pytest.mark.parametrize(
    "tamper, message",
    [
        (lambda d: (d / record.LOG_NAME).write_bytes(b"x"), "session.log changed after"),
        (lambda d: (d / record.INDEX_NAME).write_text("{}"), "session.index.json changed after"),
        (lambda d: (d / record.INDEX_NAME).unlink(), "session.index.json is missing"),
        (lambda d: (d / record.HASH_NAME).unlink(), "close the session"),
    ],
)
def test_load_refuses_a_changed_folder(closed, tamper, message):
    d, _ = closed
    tamper(d)
    with pytest.raises(record.RecordError, match=message):
        record.load(d)
