"""End-to-end against real tmux + zsh, on a private tmux socket with a minimal .zshrc."""

import json
import os
import shutil
import subprocess
import time

import pytest

from qa_bot import report, session

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
    s = session.Session.open("it", tmp_path / "report", extra_env={"ZDOTDIR": str(zdot)})
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
    while qa.last_done() < 7 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert qa.last_done() == 7

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
    assert subprocess.run(["shasum", "-a", "256", "-c", session.HASH_NAME], cwd=d, capture_output=True).returncode == 0
    idx = json.loads((d / session.INDEX_NAME).read_text())["execs"]
    assert [e["id"] for e in idx] == [1, 2, 3, 4, 5, 6, 8, 9]
    assert b"source " not in (d / session.LOG_NAME).read_bytes()

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
