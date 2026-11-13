"""Session's recording and completion logic against the fake pane (no tmux, no zsh)."""

import json

import pytest
from panefake import Cmd, FakeShell

from qa_bot import record
from qa_bot.pane import PaneError
from qa_bot.policy import Policy
from qa_bot.session import FLUSH_GRACE, Session, SessionError


@pytest.fixture
def shell():
    return FakeShell()


@pytest.fixture
def s(shell, tmp_path):
    return Session.start(
        "t", tmp_path / "report", shell,
        policy=Policy(kube_lookup=lambda env: {}), clock=shell.clock, sleep=shell.sleep,
    )


# ------------------------------------------------------------------ handshake


def test_start_loads_the_hooks_and_records_after_the_handshake(s, shell):
    assert shell.typed == [shell.typed[0]] and shell.typed[0].startswith("source ")
    assert shell.log == s.log_path and s.log_path.read_bytes() == b""  # source line not recorded
    assert s.state() == "idle" and (s.dir / "inputs").is_dir()


def test_failed_handshake_kills_the_pane_and_cleans_up(tmp_path):
    shell = FakeShell(handshake=False)
    with pytest.raises(SessionError, match="did not signal readiness"):
        Session.start("t", tmp_path, shell, clock=shell.clock, sleep=shell.sleep)
    assert shell.killed and not shell.runtime.exists() and shell.log is None


# ----------------------------------------------------------------------- exec


def test_exec_returns_the_finished_entry(s, shell):
    shell.plan(Cmd(b"hello\r\n", code=3, duration=1.5))
    r = s.exec("echo hello", 10)
    assert (r.status, r.entry_id, r.exit_code, r.duration_ms, r.output) == ("done", 1, 3, 1500, ["hello"])
    assert shell.now == pytest.approx(1.5)  # returned as soon as it finished
    assert [(e["id"], e["command"]) for e in s.index] == [(1, "echo hello")]


def test_stale_signal_from_a_manual_command_does_not_end_the_wait_early(s, shell):
    assert shell.human("ls") == 1  # leaves a signal nobody waited for
    shell.plan(Cmd(b"done\r\n", duration=5))
    r = s.exec("sleep 5", 10)
    assert (r.status, r.entry_id, r.duration_ms) == ("done", 2, 5000)
    assert shell.now >= 5


def test_signal_before_the_recorder_wrote_the_bytes(s, shell):
    shell.plan(Cmd(b"late\r\n", duration=1, log_lag=FLUSH_GRACE / 2))
    r = s.exec("echo late", 10)
    assert (r.status, r.entry_id, r.output) == ("done", 1, ["late"])
    assert shell.now == pytest.approx(1 + FLUSH_GRACE / 2, abs=0.05)


def test_end_marker_in_the_log_before_the_done_file(s, shell):
    shell.plan(Cmd(b"x\r\n", duration=1, done_lag=0.5))
    r = s.exec("echo x", 10)
    assert r.status == "done" and s.hooks.last_done() == 1
    assert shell.now == pytest.approx(1.5, abs=0.05)


def test_timeout_keeps_the_entry_id_and_interrupt_ends_it(s, shell):
    shell.plan(Cmd(duration=60))
    r = s.exec("sleep 60", 1)
    assert (r.status, r.entry_id, r.exit_code) == ("TIMEOUT", 1, None)
    assert shell.now == pytest.approx(1)
    assert s.state() == "running"
    assert s.interrupt() == "idle" and shell.keys == ["C-c"]
    assert s.index[0]["id"] == 1


def test_no_start_marker_is_an_explicit_outcome(s, shell):
    r = s.exec("echo ignored", 2)  # nothing planned: the hooks stay silent
    assert (r.status, r.entry_id) == ("NO_START", None)
    assert r.output[-1] == "% echo ignored"  # the pane, for diagnosis
    assert s.index[0]["id"] is None  # typed but never seen: shows up as missing


def test_busy_types_nothing(s, shell):
    shell.human_starts_typing()
    r = s.exec("echo nope", 5)
    assert r.status == "BUSY" and shell.typed[1:] == [] and s.index == []


def test_pane_failure_while_typing_leaves_no_phantom_exec(s, shell):
    shell.fail_typing = True
    with pytest.raises(PaneError):
        s.exec("echo x", 5)
    assert s.index == []


def test_poll_stops_on_no_start(s, shell):
    shell.plan(Cmd(b"PENDING\r\n"))
    p = s.poll("echo", "DONE", 1, 5, 2)
    assert (p.outcome, p.entry_ids) == ("no_start", [1])
    assert s.polls[0]["attempts"] == [1] and s.polls[0]["outcome"] == "no_start"


# ---------------------------------------------------------------------- close


def test_close_writes_a_record_with_agent_and_manual_entries(s, shell):
    shell.plan(Cmd(b"a\r\n"), Cmd(b"b\r\n"))
    s.exec("echo a", 5)
    shell.human("ls")
    s.exec("echo b", 5)
    s.exec("echo lost", 1)
    log_hash, count = s.close()
    assert count == 3 and shell.killed and not s.runtime.exists()
    idx = json.loads((s.dir / record.INDEX_NAME).read_text())["execs"]
    assert [e["id"] for e in idx] == [1, 3, None]
    rec = record.load(s.dir)
    assert [e.source for e in rec.entries] == ["agent", "manual", "agent"]
    assert [m.command for m in rec.missing] == ["echo lost"]
