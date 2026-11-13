import asyncio

import pytest

import qa_server
from qa_bot.policy import Policy, Refused
from qa_bot.session import ExecResult, PollResult


class FakeSession:
    closed = False

    def __init__(self, result):
        self.result = result
        self.calls = []

    def poll(self, command, until, interval, max_attempts, timeout):
        self.calls.append((command, until, interval, max_attempts, timeout))
        return self.result


@pytest.fixture
def fake(monkeypatch):
    def install(result):
        s = FakeSession(result)
        monkeypatch.setattr(qa_server, "_session", s)
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


# ------------------------------------------------------------ Session.poll outcomes

from pathlib import Path  # noqa: E402

from qa_bot.session import Session  # noqa: E402


def scripted(results, policy=None, runtime=Path("/nonexistent")):
    """A Session whose typing is scripted; the policy check is real."""
    s = Session("t", Path("."), runtime, "c", policy=policy or Policy(kube_lookup=lambda env: {}))
    it = iter(results)
    s.typed = []

    def run(command, timeout, poll_id=None):
        s.typed.append(command)
        return next(it)

    s._run = run
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


def test_refusal_between_attempts_is_recorded(tmp_path):
    # The human switches kube context in the pane after the first attempt.
    contexts = iter([{}, {}, {"context": "eu-prod1"}])
    s = scripted([done(1, "a"), done(2, "b")], Policy(kube_lookup=lambda env: next(contexts)))
    with pytest.raises(Refused, match="eu-prod1"):
        s.poll("echo", "OK", 0, 3, 5, sleep=lambda _: None)
    assert s.typed == ["echo", "echo"]
    assert s.polls[0]["attempts"] == [1, 2] and s.polls[0]["outcome"] == "error"


@pytest.mark.parametrize("command", ["curl -s https://x/$(whoami)", "curl -s https://x.prod/v1"])
def test_poll_refusal_before_first_attempt_records_nothing(command):
    s = scripted([])
    with pytest.raises(Refused):
        s.poll(command, "x", 0, 3, 5, sleep=lambda _: None)
    assert s.typed == [] and s.polls == []


# ---------------------------------------------------- the policy guards Session.exec


@pytest.mark.parametrize(
    "command, pane_env",
    [
        ("kubectl get pods > out.txt", ""),
        ("psql -c 'truncate transfers'", ""),
        ("kubectl --context prod-eu get pods", ""),
        ("psql -c 'select 1'", "PGHOST=payments.prod.rds\n"),
    ],
)
def test_exec_refuses_before_typing(tmp_path, command, pane_env):
    (tmp_path / "env").write_text(pane_env)
    s = scripted([done(1)], runtime=tmp_path)
    with pytest.raises(Refused):
        s.exec(command, 5)
    assert s.typed == []


def test_exec_checks_the_live_pane_env(tmp_path):
    env = tmp_path / "env"
    seen = []
    s = scripted([done(1), done(2)], Policy(kube_lookup=lambda e: seen.append(e) or {}), runtime=tmp_path)
    env.write_text("KUBECONFIG=/a\n")
    s.exec("kubectl get pods", 5)
    env.write_text("KUBECONFIG=/b\n")  # the human switched kubeconfig in the pane
    s.exec("kubectl get pods", 5)
    assert seen == [{"KUBECONFIG": "/a"}, {"KUBECONFIG": "/b"}]
    assert s.typed == ["kubectl get pods", "kubectl get pods"]


def test_server_exec_surfaces_refusal(monkeypatch, tmp_path):
    s = scripted([done(1)], runtime=tmp_path)
    monkeypatch.setattr(qa_server, "_session", s)
    with pytest.raises(Refused, match="Background jobs"):
        asyncio.run(qa_server.exec_command(command="kubectl port-forward svc/x 8080 &"))
    assert s.typed == []
