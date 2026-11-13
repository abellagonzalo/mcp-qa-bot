"""The protocol between the zsh hooks (``hooks.zsh``) and Python, in both directions.

This module is the only Python code that knows the protocol:

- **Loading the hooks.** ``source_line`` is the line ``Session.open`` types into the
  pane. It passes the runtime dir, the ``tmux wait-for`` channel and the env keys the
  hooks publish (``ENV_KEYS``), so the shell side declares nothing on its own.
- **Markers.** Every command line is wrapped in two invisible OSC sequences that
  travel through the pty together with its output::

      ESC ] 777 ; qa ; start ; <id> ; <epoch> ; <command> BEL
      ESC ] 777 ; qa ; end   ; <id> ; <exit code> ; <epoch> BEL

  ``scan`` decodes them from the log; ``encode_start``/``encode_end`` produce the
  exact bytes the hooks print (used by the test fakes).
- **Side channels.** ``Runtime`` reads the files the hooks keep in the runtime dir:
  the pane state, the id of the last finished command and the pane env.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union

SCRIPT = Path(__file__).with_name("hooks.zsh")

# ------------------------------------------------------------------- pane env

KUBE_ENV_KEYS = ("KUBECONFIG",)
DB_ENV_KEYS = ("PGHOST", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "DATABASE_URL")
# Published at every prompt; the production guard reads the kube and DB keys, the kube
# lookup also runs in the pane's PWD.
ENV_KEYS = (*KUBE_ENV_KEYS, *DB_ENV_KEYS, "PWD")
if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in ENV_KEYS):
    raise ValueError("ENV_KEYS must be shell variable names")


def source_line(runtime: Path, channel: str) -> str:
    """The shell line that loads the hooks into the pane (the startup handshake)."""
    args = [str(SCRIPT), str(runtime), channel, *ENV_KEYS]
    return "source " + " ".join(shlex.quote(a) for a in args)


# -------------------------------------------------------------------- markers

MARKER = re.compile(rb"\x1b\]777;qa;(start|end);([^\x07\x1b]*)(?:\x07|\x1b\\)")


@dataclass(frozen=True)
class Start:
    id: str  # as printed; not necessarily numeric if forged
    epoch: float
    command: str  # normalised, see normalise_command
    offset: int  # byte offset of the marker
    end: int  # byte offset right after the marker


@dataclass(frozen=True)
class End:
    id: str
    exit_code: int | None  # None if unreadable
    epoch: float | None
    offset: int
    end: int


Marker = Union[Start, End]


def normalise_command(cmd: str) -> str:
    """Command text as it appears in markers.

    The hooks strip BEL and ESC (they would end the OSC sequence early); the tty's
    onlcr turns \\n into \\r\\n on the way to the log.
    """
    cmd = cmd.replace("\r\n", "\n").replace("\r", "\n")
    return cmd.replace("\x07", "").replace("\x1b", "").strip()


def _float(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def scan(log: bytes) -> Iterator[Marker]:
    """Every marker in the log, in order, decoded (nesting is the caller's business)."""
    for m in MARKER.finditer(log):
        payload = m.group(2).decode("utf-8", errors="replace")
        parts = payload.split(";", 2)
        if m.group(1) == b"start":
            yield Start(
                id=parts[0],
                epoch=(_float(parts[1]) if len(parts) > 1 else None) or 0.0,
                command=normalise_command(parts[2]) if len(parts) > 2 else "",
                offset=m.start(),
                end=m.end(),
            )
        else:
            try:
                code: int | None = int(parts[1])
            except (IndexError, ValueError):
                code = None
            yield End(
                id=parts[0],
                exit_code=code,
                epoch=_float(parts[2]) if len(parts) > 2 else None,
                offset=m.start(),
                end=m.end(),
            )


def encode_start(id: int, epoch: float, command: str) -> bytes:
    """The start marker exactly as the hooks print it (before the tty's onlcr)."""
    command = command.replace("\x07", "").replace("\x1b", "")
    return b"\x1b]777;qa;start;%d;%.6f;%s\x07" % (id, epoch, command.encode())


def encode_end(id: int, exit_code: int, epoch: float) -> bytes:
    """The end marker exactly as the hooks print it."""
    return b"\x1b]777;qa;end;%d;%d;%.6f\x07" % (id, exit_code, epoch)


# ---------------------------------------------------------------- side channels

STATE_FILE = "state"  # idle | running | typing
DONE_FILE = "done"  # id of the last finished command
ENV_FILE = "env"  # KEY=value lines for ENV_KEYS, refreshed at every prompt


def parse_env(text: str) -> dict[str, str]:
    """``KEY=value`` lines -> dict; empty values are dropped."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and value:
            env[key] = value
    return env


@dataclass(frozen=True)
class Runtime:
    """The hooks' side-channel files for one pane (never printed to the terminal)."""

    dir: Path

    def state(self) -> str:
        """idle | running | typing, or unknown before the hooks wrote it."""
        try:
            return (self.dir / STATE_FILE).read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def last_done(self) -> int:
        """Id of the last command whose end marker the hooks printed (0 if none)."""
        try:
            return int((self.dir / DONE_FILE).read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def env(self) -> dict[str, str]:
        """The pane shell's env as published at the last prompt ({} if not yet)."""
        try:
            return parse_env((self.dir / ENV_FILE).read_text())
        except OSError:
            return {}
