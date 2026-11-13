"""End-to-end against real tmux + zsh, on a private tmux socket with a minimal .zshrc."""

import json
import os
import shutil
import subprocess
import time

import pytest

from qa_bot import record, report, session
from qa_bot.policy import Policy, Refused

pytestmark = pytest.mark.skipif(
    not (shutil.which("tmux") and shutil.which("zsh")), reason="needs tmux and zsh"
)

ZSHRC = """
PS1='%# '
alias e=echo
noisy_precmd() { print -n '\\e]0;title\\a' }
precmd_functions+=(noisy_precmd)
setopt noclobber
"""


@pytest.fixture
def qa(tmp_path, monkeypatch):
    socket = f"qa-bot-test-{os.getpid()}"
    monkeypatch.setenv("QA_BOT_TMUX_SOCKET", socket)
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    (zdot / ".zshrc").write_text(ZSHRC)
    # No kube lookup: the developer's own current kube context must not affect the test.
    policy = Policy(kube_lookup=lambda env: {})
    s = session.Session.open("it", tmp_path / "report", extra_env={"ZDOTDIR": str(zdot)}, policy=policy)
    yield s
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def human_types(s, text, enter=True):
    session.tmux("send-keys", "-t", s.target, "-l", "--", text)
    if enter:
        session.tmux("send-keys", "-t", s.target, "Enter")


def wait_state(s, state, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if s.state() == state:
            return
        time.sleep(0.05)
    raise AssertionError(f"state stayed {s.state()!r}")


def test_full_session(qa):
    r1 = qa.exec("echo hello; echo 'semi;'", timeout=10)
    assert (r1.status, r1.entry_id, r1.exit_code, r1.output) == ("done", 1, 0, ["hello", "semi;"])

    r2 = qa.exec("grep nothing /dev/null", timeout=10)
    assert (r2.entry_id, r2.exit_code) == (2, 1)

    r3 = qa.exec("cat <<'SQL'\nselect 1;\n-- done\nSQL", timeout=10)
    assert r3.output == ["select 1;", "-- done"]

    r3b = qa.exec("echo one \\\n  two \\\n  three", timeout=10)
    assert r3b.output == ["one two three"]
    r3c = qa.exec("cat \\\n  <<'SQL'\nselect *\n  from t;\nSQL", timeout=10)
    assert r3c.output == ["select *", "  from t;"]

    r4 = qa.exec("printf 201", timeout=10)
    assert r4.output == ["201"]

    # The human runs a command; it gets the next id and leaves a stale wait-for signal.
    human_types(qa, "e manual")
    deadline = time.monotonic() + 5
    while qa.hooks.last_done() < 7 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert qa.hooks.last_done() == 7

    # Stale signal must not make exec return before its own command finished.
    t0 = time.monotonic()
    r6 = qa.exec("sleep 1", timeout=10)
    assert r6.status == "done" and r6.entry_id == 8 and time.monotonic() - t0 >= 0.9

    # Human halfway through typing -> BUSY, nothing typed.
    human_types(qa, "echo half", enter=False)
    wait_state(qa, "typing")
    assert qa.exec("echo nope", timeout=5).status == "BUSY"
    session.tmux("send-keys", "-t", qa.target, "C-u")
    wait_state(qa, "idle")

    # Timeout leaves the command running; interrupt brings the pane back.
    r7 = qa.exec("sleep 30", timeout=1)
    assert r7.status == "TIMEOUT" and r7.entry_id == 9
    assert qa.interrupt() == "idle"

    assert "State" not in qa.peek(5)  # plain pane text

    log_hash, count = qa.close()
    assert count == 9
    d = qa.dir
    assert subprocess.run(["shasum", "-a", "256", "-c", record.HASH_NAME], cwd=d, capture_output=True).returncode == 0
    idx = json.loads((d / record.INDEX_NAME).read_text())["execs"]
    assert [e["id"] for e in idx] == [1, 2, 3, 4, 5, 6, 8, 9]
    assert b"source " not in (d / record.LOG_NAME).read_bytes()

    path, warnings = report.render_report(
        d,
        {
            "ticket": "IT-1",
            "tests": [
                {"id": "T1", "goal": "echo", "expected": "hello", "verdict": "pass", "evidence": [1, 4]},
            ],
        },
    )
    assert warnings == ["Manual entries no verdict cites: #7 (`e manual`)."]
    md = path.read_text()
    assert "> - Manual entries no verdict cites: #7 (`e manual`)." in md
    assert "Manual entries during this test" not in md  # #7 is after #4
    assert "## Timeline" not in md


def test_multiline_loop_and_poll(qa):
    loop = "for i in 1 2 3; do\n  echo \"n=$i\"\ndone"
    r1 = qa.exec(loop, timeout=10)
    assert (r1.status, r1.entry_id, r1.exit_code, r1.output) == ("done", 1, 0, ["n=1", "n=2", "n=3"])

    state = qa.dir / "inputs" / "state.txt"
    state.write_text("PENDING\n")
    sleeps = []

    def sleep(seconds):  # runs between attempts: the state flips before attempt 3
        sleeps.append(seconds)
        if len(sleeps) == 2:
            state.write_text("PROCESSED\n")

    p = qa.poll("cat inputs/state.txt", "PROCESSED", 0.2, 5, 10, sleep=sleep)
    assert (p.poll_id, p.outcome, p.entry_ids) == (1, "matched", [2, 3, 4])
    assert p.attempts[-1].output == ["PROCESSED"]

    p2 = qa.poll("cat inputs/state.txt", "NEVER", 0.1, 2, 10)
    assert (p2.poll_id, p2.outcome, p2.entry_ids) == (2, "exhausted", [5, 6])

    qa.close()
    doc = json.loads((qa.dir / record.INDEX_NAME).read_text())
    assert [(g["poll_id"], g["attempts"], g["outcome"]) for g in doc["polls"]] == [
        (1, [2, 3, 4], "matched"),
        (2, [5, 6], "exhausted"),
    ]
    assert [e.get("poll_id") for e in doc["execs"]] == [None, 1, 1, 1, 2, 2]

    path, warnings = report.render_report(
        qa.dir,
        {"tests": [
            {"id": "T1", "goal": "loop", "expected": "3 lines", "verdict": "pass", "evidence": [1]},
            {"id": "T2", "goal": "poll", "expected": "PROCESSED", "verdict": "pass", "evidence": [3]},
        ]},
    )
    assert warnings == []
    md = path.read_text()
    assert "### 🤖 #2–#4 · " in md and "polled 3× over" in md and "until /PROCESSED/ (matched)" in md
    assert "[attempt 1 · " in md and "[attempt 3 · " in md and "[attempt 2 · " not in md
    assert "$ for i in 1 2 3; do\n  echo \"n=$i\"\ndone\nn=1\nn=2\nn=3" in md


def test_policy_sees_env_the_human_sets_in_the_pane(qa):
    assert qa.exec("echo before", timeout=10).entry_id == 1
    human_types(qa, "export PGHOST=payments.prod.rds")
    wait_state(qa, "idle")
    deadline = time.monotonic() + 5  # the hooks publish the env at the next prompt
    while "PGHOST" not in qa.hooks.env():
        assert time.monotonic() < deadline, "hooks never published PGHOST"
        time.sleep(0.05)
    with pytest.raises(Refused, match="PGHOST"):
        qa.exec("echo after", timeout=10)
    with pytest.raises(Refused, match="PGHOST"):
        qa.poll("echo after", "x", 0.1, 2, 10)
    assert qa.polls == []
    qa.close()
    idx = json.loads((qa.dir / record.INDEX_NAME).read_text())["execs"]
    assert [e["command"] for e in idx] == ["echo before"]


def test_hooks_publish_every_env_key_python_declares(qa):
    from qa_bot import hooks

    human_types(qa, "export PGSERVICEFILE=/tmp/pg_service.conf PGDATABASE=payments")
    wait_state(qa, "idle")
    deadline = time.monotonic() + 5
    while "PGSERVICEFILE" not in qa.hooks.env():
        assert time.monotonic() < deadline, "hooks never published PGSERVICEFILE"
        time.sleep(0.05)
    published = (qa.runtime / hooks.ENV_FILE).read_text().splitlines()
    assert [line.split("=", 1)[0] for line in published] == list(hooks.ENV_KEYS)
    assert qa.hooks.env()["PGDATABASE"] == "payments"
