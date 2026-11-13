"""The terminal pane a session types into, behind a small interface.

``Session`` only needs to type a line, send a key, look at the screen and wait for
the hooks' "a command finished" signal. ``TmuxPane`` does that with tmux; the tests
use an in-memory fake (``tests/panefake.py``) that plays the shell + hooks side, so
the completion logic in ``Session`` can be tested deterministically.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Protocol


class PaneError(RuntimeError):
    pass


class Pane(Protocol):
    channel: str  # the hooks signal this channel when a command finished
    attach_command: str  # how a human watches / types along

    def type_line(self, text: str) -> None:
        """Paste ``text`` exactly as written (bracketed paste) and press Enter."""

    def send_key(self, key: str) -> None:
        """Send one key, in tmux key syntax (``C-c``, ``C-u``, ``Enter``)."""

    def capture(self, lines: int) -> str:
        """The last ``lines`` lines of what the pane shows."""

    def wait(self, timeout: float) -> bool:
        """Block until the hooks signal a finished command; False on timeout.

        A signal sent while nobody was waiting is remembered, so it is never lost,
        but it can be stale: callers must re-check the log and the hooks' state.
        """

    def record(self, log: Path) -> None:
        """Start appending everything the pane receives to ``log``."""

    def stop_recording(self) -> None: ...

    def kill(self) -> None: ...


# ------------------------------------------------------------------------ tmux


def tmux_base() -> list[str]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise PaneError("tmux is not installed (brew install tmux).")
    socket = os.environ.get("QA_BOT_TMUX_SOCKET")
    return [tmux, "-L", socket] if socket else [tmux]


def tmux(*args: str, input: str | None = None, timeout: float = 10, check: bool = True) -> str:
    result = subprocess.run(
        [*tmux_base(), *args], input=input, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise PaneError(f"tmux {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def has_session(name: str) -> bool:
    result = subprocess.run(
        [*tmux_base(), "has-session", "-t", f"={name}"], capture_output=True, timeout=5
    )
    return result.returncode == 0


class TmuxPane:
    """The single pane of a detached tmux session; ``channel`` is its wait-for channel."""

    def __init__(self, name: str, channel: str):
        self.name = name
        self.channel = channel
        self.target = f"={name}:"
        socket = os.environ.get("QA_BOT_TMUX_SOCKET")
        sock = f"-L {shlex.quote(socket)} " if socket else ""
        self.attach_command = f"tmux {sock}attach -t {shlex.quote(name)}"

    @classmethod
    def start(
        cls, name: str, channel: str, cwd: Path, command: list[str], env: dict[str, str] | None = None
    ) -> "TmuxPane":
        env_args: list[str] = []
        for key, value in (env or {}).items():
            env_args += ["-e", f"{key}={value}"]
        tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50", "-c", str(cwd), *env_args, *command)
        return cls(name, channel)

    def type_line(self, text: str) -> None:
        tmux("load-buffer", "-b", self.channel, "-", input=text)
        tmux("paste-buffer", "-b", self.channel, "-d", "-p", "-t", self.target)
        self.send_key("Enter")

    def send_key(self, key: str) -> None:
        tmux("send-keys", "-t", self.target, key)

    def send_text(self, text: str) -> None:
        """Type ``text`` literally, key by key, like a human would (no Enter)."""
        tmux("send-keys", "-t", self.target, "-l", "--", text)

    def capture(self, lines: int) -> str:
        text = tmux("capture-pane", "-p", "-J", "-t", self.target, "-S", f"-{max(1, lines)}")
        return "\n".join(text.rstrip("\n").splitlines()[-lines:])

    def wait(self, timeout: float) -> bool:
        if timeout <= 0:
            return False
        try:
            subprocess.run([*tmux_base(), "wait-for", self.channel], capture_output=True, timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def record(self, log: Path) -> None:
        tmux("pipe-pane", "-o", "-t", self.target, f"cat >> {shlex.quote(str(log))}")

    def stop_recording(self) -> None:
        tmux("pipe-pane", "-t", self.target, check=False)  # no command = stop piping

    def kill(self) -> None:
        tmux("kill-session", "-t", f"={self.name}", check=False)
