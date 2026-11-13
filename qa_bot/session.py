"""One QA session = one tmux session running the user's interactive zsh, recorded.

Mechanics:
- ``open`` starts ``zsh -l`` in a detached tmux session, types ``source hooks.zsh``
  and waits for the hooks' first ``tmux wait-for`` signal (the user's .zshrc has
  finished loading and the hooks are active). Only then ``pipe-pane`` starts
  streaming the pane into ``session.log``, so the ``source`` line is not recorded.
- ``exec`` pastes the command into the pane (bracketed paste, so ``;`` and heredocs
  arrive exactly as written), presses Enter and waits on ``tmux wait-for`` until the
  hooks report that this command finished.
- ``poll`` runs ``exec`` once per attempt until the output matches a regex; every
  attempt is a normal entry, and the attempts are recorded as one poll group.
- ``close`` stops ``pipe-pane`` and writes ``session.index.json`` (exec records and
  poll groups) and ``session.log.sha256``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import transcript

HOOKS = Path(__file__).with_name("hooks.zsh")
LOG_NAME = "session.log"
INDEX_NAME = "session.index.json"
HASH_NAME = "session.log.sha256"
HANDSHAKE_TIMEOUT = 30.0
FLUSH_GRACE = 2.0  # how long to wait for pipe-pane to deliver bytes after a signal


class SessionError(RuntimeError):
    pass


def tmux_base() -> list[str]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise SessionError("tmux is not installed (brew install tmux).")
    socket = os.environ.get("QA_BOT_TMUX_SOCKET")
    return [tmux, "-L", socket] if socket else [tmux]


def tmux(*args: str, input: str | None = None, timeout: float = 10, check: bool = True) -> str:
    result = subprocess.run(
        [*tmux_base(), *args], input=input, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise SessionError(f"tmux {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def wait_for(channel: str, timeout: float) -> bool:
    """Block on `tmux wait-for <channel>`; False on timeout.

    tmux remembers a signal sent while nobody was waiting, so a signal can never be
    lost; it can be stale, which is why callers re-check the hooks' state afterwards.
    """
    if timeout <= 0:
        return False
    try:
        subprocess.run(
            [*tmux_base(), "wait-for", channel], capture_output=True, timeout=timeout
        )
        return True
    except subprocess.TimeoutExpired:
        return False


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-").lower()
    return s[:40] or "qa"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_hash_file(directory: Path) -> str:
    log_hash = sha256_file(directory / LOG_NAME)
    index_hash = sha256_file(directory / INDEX_NAME)
    (directory / HASH_NAME).write_text(f"{log_hash}  {LOG_NAME}\n{index_hash}  {INDEX_NAME}\n")
    return log_hash


def verify_hash_file(directory: Path) -> str:
    """Return the log's sha256 if session.log and the index still match the hash file."""
    hash_file = directory / HASH_NAME
    if not hash_file.exists():
        raise SessionError(f"{hash_file} not found: close the session before rendering the report.")
    expected: dict[str, str] = {}
    for line in hash_file.read_text().splitlines():
        digest, _, name = line.partition("  ")
        expected[name.strip()] = digest.strip()
    for name in (LOG_NAME, INDEX_NAME):
        path = directory / name
        if not path.exists():
            raise SessionError(f"Integrity check failed: {name} is missing.")
        if sha256_file(path) != expected.get(name):
            raise SessionError(f"Integrity check failed: {name} changed after the session was closed.")
    return expected[LOG_NAME]


@dataclass
class ExecResult:
    status: str  # done | TIMEOUT | BUSY
    entry_id: int | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output: list[str] = field(default_factory=list)


@dataclass
class PollResult:
    poll_id: int
    outcome: str  # matched | exhausted | busy | timeout | interrupted
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
    runtime: Path
    channel: str
    index: list[dict] = field(default_factory=list)
    polls: list[dict] = field(default_factory=list)
    closed: bool = False

    # ---------------------------------------------------------------- paths

    @property
    def log_path(self) -> Path:
        return self.dir / LOG_NAME

    @property
    def target(self) -> str:
        return f"={self.name}:"

    @property
    def attach_command(self) -> str:
        socket = os.environ.get("QA_BOT_TMUX_SOCKET")
        sock = f"-L {shlex.quote(socket)} " if socket else ""
        return f"tmux {sock}attach -t {shlex.quote(self.name)}"

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, name: str, directory: str | Path, extra_env: dict[str, str] | None = None) -> "Session":
        session_name = slug(name)
        directory = Path(directory).expanduser().resolve()
        if (directory / LOG_NAME).exists():
            raise SessionError(f"{directory / LOG_NAME} already exists; use a new folder per session.")
        if _has_session(session_name):
            raise SessionError(f"tmux session '{session_name}' already exists; pick another name.")
        zsh = shutil.which("zsh")
        if not zsh:
            raise SessionError("zsh is not installed.")

        directory.mkdir(parents=True, exist_ok=True)
        (directory / "inputs").mkdir(exist_ok=True)
        runtime = Path(tempfile.mkdtemp(prefix=f"mcp-qa-bot-{session_name}-"))
        channel = f"qa-bot-{session_name}-{secrets.token_hex(4)}"
        session = cls(session_name, directory, runtime, channel)

        env_args: list[str] = []
        for key, value in (extra_env or {}).items():
            env_args += ["-e", f"{key}={value}"]
        tmux(
            "new-session", "-d", "-s", session_name, "-x", "200", "-y", "50",
            "-c", str(directory), *env_args, zsh, "-l",
        )
        try:
            session._type_line(
                f"source {shlex.quote(str(HOOKS))} {shlex.quote(str(runtime))} {shlex.quote(channel)}"
            )
            if not wait_for(channel, HANDSHAKE_TIMEOUT):
                raise SessionError(
                    f"The shell did not signal readiness within {HANDSHAKE_TIMEOUT:g}s "
                    "(is the .zshrc waiting for input?)."
                )
            session.log_path.touch()
            tmux("pipe-pane", "-o", "-t", session.target, f"cat >> {shlex.quote(str(session.log_path))}")
        except Exception:
            tmux("kill-session", "-t", f"={session_name}", check=False)
            shutil.rmtree(runtime, ignore_errors=True)
            raise
        return session

    def close(self, kill: bool = True) -> tuple[str, int]:
        if self.closed:
            raise SessionError("Session already closed.")
        tmux("pipe-pane", "-t", self.target, check=False)  # no command = stop piping
        _wait_stable(self.log_path)
        (self.dir / INDEX_NAME).write_text(json.dumps({"session": self.name, "execs": self.index, "polls": self.polls}, indent=2) + "\n")
        log_hash = write_hash_file(self.dir)
        self.closed = True
        if kill:
            tmux("kill-session", "-t", f"={self.name}", check=False)
        shutil.rmtree(self.runtime, ignore_errors=True)
        entries = transcript.parse(self.log_path.read_bytes()).entries
        return log_hash, len(entries)

    # ---------------------------------------------------------------- state

    def state(self) -> str:
        """idle | running | typing (from the hooks)."""
        try:
            return (self.runtime / "state").read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def last_done(self) -> int:
        try:
            return int((self.runtime / "done").read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def pane_env_path(self) -> Path:
        return self.runtime / "env"

    def peek(self, lines: int = 40) -> str:
        text = tmux("capture-pane", "-p", "-J", "-t", self.target, "-S", f"-{max(1, lines)}")
        return "\n".join(text.rstrip("\n").splitlines()[-lines:])

    def interrupt(self, settle: float = 3.0) -> str:
        tmux("send-keys", "-t", self.target, "C-c")
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline and self.state() == "running":
            wait_for(self.channel, min(0.5, deadline - time.monotonic()))
        return self.state()

    # ----------------------------------------------------------------- exec

    def _type_line(self, text: str) -> None:
        tmux("load-buffer", "-b", self.channel, "-", input=text)
        tmux("paste-buffer", "-b", self.channel, "-d", "-p", "-t", self.target)
        tmux("send-keys", "-t", self.target, "Enter")

    def exec(self, command: str, timeout: float, poll_id: int | None = None) -> ExecResult:
        if self.closed:
            raise SessionError("Session is closed.")
        state = self.state()
        if state != "idle":
            return ExecResult(status="BUSY", output=self.peek(20).splitlines())

        offset = self.log_path.stat().st_size
        typed_at = datetime.now().astimezone()
        record = {"id": None, "command": command, "typed_at": typed_at.isoformat(timespec="milliseconds")}
        if poll_id is not None:
            record["poll_id"] = poll_id
        self.index.append(record)
        self._type_line(command)

        deadline = time.monotonic() + timeout
        entry: transcript.Entry | None = None
        while True:
            entry = self._find_entry(offset)
            if entry is not None and record["id"] is None:
                record["id"] = entry.id
            if entry is not None and entry.finished and self.last_done() >= entry.id:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if entry is not None and entry.finished:
                # Hooks wrote the end marker; give pipe-pane/state a moment to catch up.
                time.sleep(0.02)
                continue
            if wait_for(self.channel, remaining):
                # The signal may arrive before pipe-pane has written the bytes.
                grace = time.monotonic() + FLUSH_GRACE
                while time.monotonic() < grace:
                    entry = self._find_entry(offset)
                    if entry is not None and entry.finished:
                        break
                    time.sleep(0.02)

        if entry is None:
            return ExecResult(status="TIMEOUT", output=self.peek(40).splitlines())
        return ExecResult(
            status="done" if entry.finished else "TIMEOUT",
            entry_id=entry.id,
            exit_code=entry.exit_code,
            duration_ms=entry.duration_ms,
            output=entry.lines,
        )

    def poll(
        self,
        command: str,
        until: str,
        interval: float,
        max_attempts: int,
        timeout: float,
        before_attempt: Callable[[], None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> PollResult:
        """Run ``command`` up to ``max_attempts`` times, ``interval`` seconds apart, until
        its output matches the ``until`` regex.

        Each attempt goes through ``exec`` (one recorded entry each) after
        ``before_attempt`` (the caller's per-attempt safety check, which may raise).
        Stops early when the pane is busy, an attempt times out or is interrupted
        (Ctrl-C). The group is recorded in the index whatever the outcome.
        """
        pattern = re.compile(until, re.M)
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
        started = time.monotonic()
        outcome = "exhausted"
        for n in range(max_attempts):
            if n:
                sleep(interval)
            before_attempt()
            result = self.exec(command, timeout, poll_id=group["poll_id"])
            if result.status == "BUSY":
                outcome = "busy"
                break
            attempts.append(result)
            if result.entry_id is not None:
                group["attempts"].append(result.entry_id)
            if result.status == "TIMEOUT":
                outcome = "timeout"
                break
            if result.exit_code in INTERRUPTED_EXIT_CODES:
                outcome = "interrupted"
                break
            if pattern.search("\n".join(result.output)):
                outcome = "matched"
                break
        group["outcome"] = outcome
        return PollResult(group["poll_id"], outcome, attempts, time.monotonic() - started)

    def _find_entry(self, offset: int) -> transcript.Entry | None:
        """First top-level entry that started after ``offset`` in the log."""
        with self.log_path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        parsed = transcript.parse(data)
        return parsed.entries[0] if parsed.entries else None


def _has_session(name: str) -> bool:
    result = subprocess.run(
        [*tmux_base(), "has-session", "-t", f"={name}"], capture_output=True, timeout=5
    )
    return result.returncode == 0


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
