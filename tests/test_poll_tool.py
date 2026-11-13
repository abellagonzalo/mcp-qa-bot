import asyncio

import pytest

import qa_server
from qa_bot.limits import CommandNotAllowed
from qa_bot.session import ExecResult, PollResult


class FakeSession:
    closed = False

    def __init__(self, result):
        self.result = result
        self.calls = []

    def poll(self, command, until, interval, max_attempts, timeout, before_attempt):
        self.calls.append((command, until, interval, max_attempts, timeout))
        before_attempt()
        return self.result


@pytest.fixture
def fake(monkeypatch):
    def install(result):
        s = FakeSession(result)
        monkeypatch.setattr(qa_server, "_session", s)
        monkeypatch.setattr(qa_server, "_check_production", lambda session, command: None)
        return s
    return install


def run(**kw):
    return asyncio.run(qa_server.poll(**kw))


def test_matched_output(fake):
    attempts = [
        ExecResult("done", 5, 0, 120, ['{"status": "PENDING"}']),
        ExecResult("done", 6, 0, 110, ['{"status": "PROCESSED"}']),
    ]
    s = fake(PollResult(1, "matched", attempts, 21.4))
    out = run(command="curl -s https://x | jq .", until="PROCESSED", interval=0.1, max_attempts=500)
    assert out.splitlines() == [
        "P1 · matched after 2 attempt(s) in 21 s · entries #5, #6",
        "Last attempt #6 · exit 0 · 110 ms",
        '1│ {"status": "PROCESSED"}',
    ]
    # interval and attempts are clamped
    assert s.calls[0][2:4] == (1.0, qa_server.MAX_POLL_ATTEMPTS)


def test_busy_before_any_attempt(fake):
    fake(PollResult(2, "busy", [], 0.0))
    out = run(command="echo x", until="x")
    assert out.startswith("P2 · busy after 0 attempt(s)") and "BUSY" in out


def test_invalid_regex(fake):
    fake(PollResult(1, "matched", [], 0.0))
    with pytest.raises(ValueError, match="not a valid regex"):
        run(command="echo x", until="(")


def test_same_hard_limits_as_exec(fake):
    s = fake(PollResult(1, "matched", [], 0.0))
    with pytest.raises(CommandNotAllowed):
        run(command="curl -s https://x/$(whoami)", until="x")
    assert s.calls == []


def test_production_check_runs_before_each_attempt(fake, monkeypatch):
    s = fake(PollResult(1, "matched", [], 0.0))
    seen = []
    monkeypatch.setattr(qa_server, "_check_production", lambda session, command: seen.append(command))
    run(command="echo x", until="x")
    assert seen == ["echo x"] and len(s.calls) == 1


# ------------------------------------------------------------ Session.poll outcomes

from pathlib import Path  # noqa: E402

from qa_bot.safety import ProductionRefused  # noqa: E402
from qa_bot.session import Session  # noqa: E402


def scripted(results):
    s = Session("t", Path("."), Path("."), "c")
    it = iter(results)
    s.exec = lambda command, timeout, poll_id=None: next(it)
    return s


def done(eid, out="x", code=0):
    return ExecResult("done", eid, code, 5, [out])


@pytest.mark.parametrize(
    "results, outcome, ids",
    [
        ([done(1, "a"), done(2, "b")], "exhausted", [1, 2]),
        ([done(1, "a"), done(2, "OK")], "matched", [1, 2]),
        ([done(1, "a"), ExecResult("BUSY", output=["% typing"])], "busy", [1]),
        ([done(1, "a"), ExecResult("TIMEOUT", 2, None, None, ["…"])], "timeout", [1, 2]),
        ([done(1, "a", code=130)], "interrupted", [1]),
    ],
)
def test_session_poll_outcomes(results, outcome, ids):
    s = scripted(results)
    r = s.poll("echo", "^OK$", 0, 2, 5, sleep=lambda _: None)
    assert (r.outcome, r.entry_ids) == (outcome, ids)
    assert s.polls == [{"poll_id": 1, "command": "echo", "until": "^OK$", "interval": 0,
                        "max_attempts": 2, "attempts": ids, "outcome": outcome}]


def test_refusal_between_attempts_is_recorded():
    s = scripted([done(1, "a"), done(2, "b")])
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:
            raise ProductionRefused("prod")

    with pytest.raises(ProductionRefused):
        s.poll("echo", "OK", 0, 3, 5, check, sleep=lambda _: None)
    assert s.polls[0]["attempts"] == [1] and s.polls[0]["outcome"] == "error"
