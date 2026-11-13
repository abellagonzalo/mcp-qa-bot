"""An in-memory pane: plays zsh + the hooks + the recorder on a virtual clock.

It implements ``qa_bot.pane.Pane`` and doubles as the session's clock, so
``Session``'s completion logic runs deterministically and instantly:

    shell = FakeShell()
    s = Session.start("t", tmp_path, shell, clock=shell.clock, sleep=shell.sleep)
    shell.plan(Cmd(b"hello\\r\\n", duration=1.0))   # how the next typed line behaves
    s.exec("echo hello", 5)

Everything the hooks would publish goes where the real ones put it: marker bytes into
the recorded log (built by ``hooks.encode_*`` via ``logfake``), and the state/done
files into the runtime dir named on the ``source`` line. Each scheduled event happens
at a virtual time; ``wait`` and ``sleep`` advance the clock and run due events.
"""

from __future__ import annotations

import heapq
import itertools
import shlex
from dataclasses import dataclass
from pathlib import Path

from logfake import PROMPT, PROMPT_SP, end, start

from qa_bot import hooks
from qa_bot.pane import PaneError

T0 = 1_700_000_000.0  # epoch at virtual time 0


@dataclass
class Cmd:
    """How the shell reacts to the next typed line.

    ``duration``: seconds from start to the end marker being printed. After that, the
    recorder writes the bytes to the log after ``log_lag`` and the hooks write the done
    file, set the state to idle and signal after ``done_lag``. Either can come first.
    """

    output: bytes = b""
    code: int = 0
    duration: float = 0.1
    log_lag: float = 0.0
    done_lag: float = 0.0


class FakeShell:
    channel = "fake-channel"
    attach_command = "fake attach"

    def __init__(self, handshake: bool = True):
        self.handshake = handshake
        self.now = 0.0
        self.signalled = False
        self.fail_typing = False
        self.killed = False
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.runtime: Path | None = None
        self.log: Path | None = None
        self._plans: list[Cmd] = []
        self._events: list = []
        self._seq = itertools.count()
        self._ids = itertools.count(1)
        self._running: int | None = None

    # -------------------------------------------------------------- clock

    def clock(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self._run_until(self.now + max(0.0, dt))

    def _at(self, delay: float, fn) -> None:
        heapq.heappush(self._events, (self.now + delay, next(self._seq), fn))

    def _run_until(self, t: float, stop_on_signal: bool = False) -> bool:
        while self._events and self._events[0][0] <= t:
            when, _, fn = heapq.heappop(self._events)
            self.now = max(self.now, when)
            fn()
            if stop_on_signal and self.signalled:
                return True
        self.now = max(self.now, t)
        return False

    # ------------------------------------------------- hooks' side effects

    def _append(self, data: bytes) -> None:
        if self.log is not None:  # only once recording started
            with self.log.open("ab") as fh:
                fh.write(data)

    def _write(self, name: str, text: str) -> None:
        (self.runtime / name).write_text(text)

    def _signal(self) -> None:
        self.signalled = True  # tmux remembers a signal nobody waited for

    def _start(self, command: str) -> int:
        eid = next(self._ids)
        self._running = eid
        self._write(hooks.STATE_FILE, "running")
        echo = command.encode().replace(b"\n", b"\r\n") + b"\x1b[?2004l\r\r\n"
        self._append(echo + start(eid, command, T0 + self.now))
        return eid

    def _finish(self, eid: int, cmd: Cmd, code: int) -> None:
        if self._running != eid:
            return  # already finished (interrupted)
        self._running = None
        ended = T0 + self.now

        def precmd_side_channels():  # same order as hooks.zsh: state, done, signal
            self._write(hooks.STATE_FILE, "idle")
            self._write(hooks.DONE_FILE, str(eid))
            self._signal()

        self._at(cmd.log_lag, lambda: self._append(cmd.output + PROMPT_SP + end(eid, code, ended) + PROMPT))
        self._at(cmd.done_lag, precmd_side_channels)

    # ------------------------------------------------------- test controls

    def plan(self, *cmds: Cmd) -> None:
        """Queue how the next typed lines behave (no plan: the hooks stay silent)."""
        self._plans += cmds

    def human(self, command: str, output: bytes = b"", code: int = 0) -> int:
        """The human runs a command to completion right now (leaves a stale signal)."""
        eid = self._start(command)
        self._append(output + PROMPT_SP + end(eid, code, T0 + self.now) + PROMPT)
        self._write(hooks.STATE_FILE, "idle")
        self._write(hooks.DONE_FILE, str(eid))
        self._running = None
        self._signal()
        return eid

    def human_starts_typing(self) -> None:
        self._write(hooks.STATE_FILE, "typing")

    # --------------------------------------------------------- Pane interface

    def type_line(self, text: str) -> None:
        if self.fail_typing:
            raise PaneError("tmux paste-buffer failed: no server running")
        self.typed.append(text)
        if self.runtime is None:  # the source line: load the hooks
            words = shlex.split(text)
            assert words[:2] == ["source", str(hooks.SCRIPT)] and words[3] == self.channel
            self.runtime = Path(words[2])
            if self.handshake:
                self._write(hooks.STATE_FILE, "idle")
                self._signal()
            return
        if not self._plans:
            return
        cmd = self._plans.pop(0)
        eid = self._start(text)
        self._at(cmd.duration, lambda: self._finish(eid, cmd, cmd.code))

    def send_key(self, key: str) -> None:
        self.keys.append(key)
        if key == "C-c" and self._running is not None:
            self._finish(self._running, Cmd(b"^C\r\n"), 130)

    def capture(self, lines: int) -> str:
        return "\n".join(["% " + t for t in self.typed][-lines:])

    def wait(self, timeout: float) -> bool:
        if timeout <= 0:
            return False
        if not self.signalled:
            self._run_until(self.now + timeout, stop_on_signal=True)
        hit, self.signalled = self.signalled, False
        return hit

    def record(self, log: Path) -> None:
        self.log = log

    def stop_recording(self) -> None:
        self.log = None

    def kill(self) -> None:
        self.killed = True
