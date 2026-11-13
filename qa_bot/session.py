"""One QA session = one pane running the user's interactive zsh, recorded.

Mechanics:
- ``open`` starts ``zsh -l`` in a detached tmux session (``pane.TmuxPane``), types the
  line from ``hooks.source_line`` and waits for the hooks' first signal (the user's
  .zshrc has finished loading and the hooks are active). Only then does the pane
  start recording into ``session.log``, so the ``source`` line is not recorded.
- ``exec`` checks the command against the exec policy (``policy.py``; refusals raise
  ``Refused`` and nothing is typed), then types the command into the pane exactly as
  written and waits until the hooks report that this command finished.
- ``poll`` runs ``exec`` once per attempt until the output matches a regex; every
  attempt is a normal entry, and the attempts are recorded as one poll group.
- ``close`` stops recording and hands the exec records and poll groups to
  ``record.write`` (``session.index.json`` and ``session.log.sha256``).

``Session`` only talks to the terminal through the ``pane.Pane`` interface and reads
time through ``clock``/``sleep``, so the completion logic runs against a fake pane in
the tests.
"""

from __future__ import annotations

import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import hooks, record, transcript
from .pane import Pane, TmuxPane, has_session
from .policy import Policy

HANDSHAKE_TIMEOUT = 30.0
FLUSH_GRACE = 2.0  # how long to wait for the log to receive the bytes after a signal
SPIN = 0.02  # re-check interval while waiting on the log or the hooks' done file


class SessionError(RuntimeError):
    pass


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-").lower()
    return s[:40] or "qa"


@dataclass
class ExecResult:
    """What happened to one typed command.

    - ``done``: it finished; ``entry_id``, ``exit_code`` and ``duration_ms`` are set.
    - ``TIMEOUT``: it started (``entry_id`` is set) and is still running.
    - ``NO_START``: it was typed but the hooks never printed its start marker (hooks
      broken, or the shell did not accept the input); ``output`` is the pane.
    - ``BUSY``: nothing was typed (a command is running or the human is typing).
    """

    status: str  # done | TIMEOUT | NO_START | BUSY
    entry_id: int | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output: list[str] = field(default_factory=list)


@dataclass
class PollResult:
    poll_id: int
    outcome: str  # matched | exhausted | busy | timeout | no_start | interrupted
    attempts: list[ExecResult]  # attempts that ran (BUSY ones are not included)
    elapsed_s: float

    @property
    def entry_ids(self) -> list[int]:
        return [a.entry_id for a in self.attempts if a.entry_id is not None]


INTERRUPTED_EXIT_CODES = {130}  # Ctrl-C


@dataclass
class Session:
    name: str
    dir: Path
    runtime: Path  # the hooks' side-channel dir (see hooks.Runtime)
    pane: Pane
    index: list[dict] = field(default_factory=list)
    polls: list[dict] = field(default_factory=list)
    closed: bool = False
    policy: Policy = field(default_factory=Policy)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    # ---------------------------------------------------------------- paths

    @property
    def log_path(self) -> Path:
        return self.dir / record.LOG_NAME

    @property
    def attach_command(self) -> str:
        return self.pane.attach_command

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(
        cls,
        name: str,
        directory: str | Path,
        extra_env: dict[str, str] | None = None,
        policy: Policy | None = None,
    ) -> "Session":
        session_name = slug(name)
        directory = Path(directory).expanduser().resolve()
        if (directory / record.LOG_NAME).exists():
            raise SessionError(f"{directory / record.LOG_NAME} already exists; use a new folder per session.")
        if has_session(session_name):
            raise SessionError(f"tmux session '{session_name}' already exists; pick another name.")
        zsh = shutil.which("zsh")
        if not zsh:
            raise SessionError("zsh is not installed.")

        directory.mkdir(parents=True, exist_ok=True)
        channel = f"qa-bot-{session_name}-{secrets.token_hex(4)}"
        pane = TmuxPane.start(session_name, channel, directory, [zsh, "-l"], extra_env)
        return cls.start(session_name, directory, pane, policy=policy or Policy())

    @classmethod
    def start(cls, name: str, directory: Path, pane: Pane, **kwargs) -> "Session":
        """Load the hooks into a fresh pane, wait for the handshake, start recording.

        On any failure the pane is killed and nothing is left behind but the folder.
        """
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "inputs").mkdir(exist_ok=True)
        runtime = Path(tempfile.mkdtemp(prefix=f"mcp-qa-bot-{name}-"))
        session = cls(name, directory, runtime, pane, **kwargs)
        try:
            pane.type_line(hooks.source_line(runtime, pane.channel))
            if not pane.wait(HANDSHAKE_TIMEOUT):
                raise SessionError(
                    f"The shell did not signal readiness within {HANDSHAKE_TIMEOUT:g}s "
                    "(is the .zshrc waiting for input?)."
                )
            session.log_path.touch()
            pane.record(session.log_path)
        except Exception:
            pane.kill()
            shutil.rmtree(runtime, ignore_errors=True)
            raise
        return session

    def close(self, kill: bool = True) -> tuple[str, int]:
        if self.closed:
            raise SessionError("Session already closed.")
        self.pane.stop_recording()
        _wait_stable(self.log_path)
        log_hash = record.write(self.dir, self.name, self.index, self.polls)
        self.closed = True
        if kill:
            self.pane.kill()
        shutil.rmtree(self.runtime, ignore_errors=True)
        entries = transcript.parse(self.log_path.read_bytes()).entries
        return log_hash, len(entries)

    # ---------------------------------------------------------------- state

    @property
    def hooks(self) -> hooks.Runtime:
        """What the shell hooks publish about the pane (state, last finished id, env)."""
        return hooks.Runtime(self.runtime)

    def state(self) -> str:
        """idle | running | typing (from the hooks)."""
        return self.hooks.state()

    def peek(self, lines: int = 40) -> str:
        return self.pane.capture(lines)

    def interrupt(self, settle: float = 3.0) -> str:
        self.pane.send_key("C-c")
        deadline = self.clock() + settle
        while self.clock() < deadline and self.state() == "running":
            self.pane.wait(min(0.5, deadline - self.clock()))
        return self.state()

    # ----------------------------------------------------------------- exec

    def _check(self, command: str) -> None:
        """Refuse (``policy.Refused``) unless the policy allows the command in the pane's current context."""
        self.policy.check(command, self.hooks.env())

    def exec(self, command: str, timeout: float) -> ExecResult:
        """Check the command against the policy, type it, and wait until it finishes."""
        if self.closed:
            raise SessionError("Session is closed.")
        self._check(command)
        return self._run(command, timeout)

    def _run(self, command: str, timeout: float, poll_id: int | None = None) -> ExecResult:
        """Type an already-checked command and wait until it finishes.

        The exec is recorded in the index only once it was typed, so a pane failure
        while typing raises without leaving a phantom "missing" exec behind.
        """
        if self.closed:
            raise SessionError("Session is closed.")
        if self.state() != "idle":
            return ExecResult(status="BUSY", output=self.peek(20).splitlines())

        offset = self.log_path.stat().st_size
        typed_at = datetime.now().astimezone()
        self.pane.type_line(command)
        entry_record = {"id": None, "command": command, "typed_at": typed_at.isoformat(timespec="milliseconds")}
        if poll_id is not None:
            entry_record["poll_id"] = poll_id
        self.index.append(entry_record)

        entry = self._wait_finished(offset, timeout)
        if entry is None:
            return ExecResult(status="NO_START", output=self.peek(40).splitlines())
        entry_record["id"] = entry.id
        return ExecResult(
            status="done" if entry.finished else "TIMEOUT",
            entry_id=entry.id,
            exit_code=entry.exit_code,
            duration_ms=entry.duration_ms,
            output=entry.lines,
        )

    def _wait_finished(self, offset: int, timeout: float) -> transcript.Entry | None:
        """Wait until the first entry after ``offset`` finished, or until ``timeout``.

        Finished means its end marker is in the log *and* the hooks' done file has
        caught up with its id. A signal can be stale (left by an earlier, e.g. manual,
        command) or arrive before the recorder wrote the bytes, so every signal is
        re-checked against the log. Returns the entry as last seen (unfinished on
        timeout), or None if its start marker never appeared.
        """
        deadline = self.clock() + timeout
        entry: transcript.Entry | None = None
        while True:
            entry = self._find_entry(offset)
            if entry is not None and entry.finished and self.hooks.last_done() >= entry.id:
                return entry
            remaining = deadline - self.clock()
            if remaining <= 0:
                return entry
            if entry is not None and entry.finished:
                # End marker is in the log; give the done file a moment to catch up.
                self.sleep(SPIN)
                continue
            if self.pane.wait(remaining):
                # The signal may arrive before the recorder has written the bytes.
                grace = self.clock() + FLUSH_GRACE
                while self.clock() < grace:
                    entry = self._find_entry(offset)
                    if entry is not None and entry.finished:
                        break
                    self.sleep(SPIN)

    def poll(
        self,
        command: str,
        until: str,
        interval: float,
        max_attempts: int,
        timeout: float,
        sleep: Callable[[float], None] | None = None,
    ) -> PollResult:
        """Run ``command`` up to ``max_attempts`` times, ``interval`` seconds apart, until
        its output matches the ``until`` regex.

        Every attempt is checked against the policy right before it is typed (the
        context can change between attempts) and is one recorded entry. A refusal of
        the first attempt raises before anything is recorded; a later refusal raises
        with the group recorded as ``error``.
        Stops early when the pane is busy, an attempt times out or is interrupted
        (Ctrl-C). The group is recorded in the index whatever the outcome.
        """
        if self.closed:
            raise SessionError("Session is closed.")
        pattern = re.compile(until, re.M)
        self._check(command)
        group = {
            "poll_id": len(self.polls) + 1,
            "command": command,
            "until": until,
            "interval": interval,
            "max_attempts": max_attempts,
            "attempts": [],
            "outcome": "error",  # overwritten unless an exception escapes
        }
        self.polls.append(group)
        attempts: list[ExecResult] = []
        started = self.clock()
        outcome = "exhausted"
        for n in range(max_attempts):
            if n:
                (sleep or self.sleep)(interval)
                self._check(command)
            result = self._run(command, timeout, poll_id=group["poll_id"])
            if result.status == "BUSY":
                outcome = "busy"
                break
            attempts.append(result)
            if result.entry_id is not None:
                group["attempts"].append(result.entry_id)
            if result.status == "TIMEOUT":
                outcome = "timeout"
                break
            if result.status == "NO_START":
                outcome = "no_start"
                break
            if result.exit_code in INTERRUPTED_EXIT_CODES:
                outcome = "interrupted"
                break
            if pattern.search("\n".join(result.output)):
                outcome = "matched"
                break
        group["outcome"] = outcome
        return PollResult(group["poll_id"], outcome, attempts, self.clock() - started)

    def _find_entry(self, offset: int) -> transcript.Entry | None:
        """First top-level entry that started after ``offset`` in the log."""
        with self.log_path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        parsed = transcript.parse(data)
        return parsed.entries[0] if parsed.entries else None


def _wait_stable(path: Path, timeout: float = 2.0) -> None:
    """Wait until the pipe-pane writer has flushed (file size stops changing)."""
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        size = path.stat().st_size if path.exists() else 0
        if size == last:
            return
        last = size
        time.sleep(0.15)
