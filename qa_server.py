#!/usr/bin/env python3
"""mcp-qa-bot: execution layer for agent-driven manual QA.

Runs shell commands in a tmux pane a human can watch (and type in), records
everything that runs there, and renders a report from verdicts it is given. It knows
nothing about QA itself; the workflow lives in the QA skill (skills/manual-qa).

Tools: preferred_tools, open_session, exec, poll, peek, interrupt, close_session, render_report.

Preferred tools (guidance for the agent, not enforced): ~/.config/qa-bot/tools.md
(system) merged with the nearest .qa-bot/tools.md at or above the session folder
(project wins per ## section). See qa_bot/tools.py.

Configuration (environment variables):
  QA_BOT_PROD_PATTERN      regex for production names (refused before every exec)
  QA_BOT_DEFAULT_TIMEOUT   seconds exec waits for a command (default 60)
  QA_BOT_EXEC_MAX_LINES    output lines exec returns to the agent (default 300)
  QA_BOT_REPORT_MAX_LINES  lines shown when an entry is cited without a range (default 200)
  QA_BOT_IMAGE_MAX_LINES   lines per evidence image before render_report warns (default 60)
  QA_BOT_IMAGE_SCALE       pixels per point of evidence images (default 1; freeze's own is 4)
  QA_BOT_TMUX_SOCKET       tmux socket name (-L); default: the user's default server
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from qa_bot import report, tools
from qa_bot.policy import Refused
from qa_bot.session import Session, SessionError

MAX_TIMEOUT = 1800.0
MAX_POLL_INTERVAL = 600.0
MAX_POLL_ATTEMPTS = 100

mcp = FastMCP("mcp-qa-bot")
_lock = asyncio.Lock()
_session: Session | None = None
_last_dir: Path | None = None


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _require_session() -> Session:
    if _session is None or _session.closed:
        raise SessionError("No open session. Call open_session first.")
    return _session


def _wait(timeout: float | None) -> float:
    wait = _env_number("QA_BOT_DEFAULT_TIMEOUT", 60) if timeout is None else timeout
    return max(1.0, min(float(wait), MAX_TIMEOUT))


def _numbered(lines: list[str], max_lines: int) -> str:
    """Output with entry line numbers (what verdicts cite), tail-capped for the agent."""
    if not lines:
        return "(no output)"
    width = len(str(len(lines)))
    start = max(0, len(lines) - max_lines)
    body = [f"{i:>{width}}│ {line}" for i, line in enumerate(lines[start:], start + 1)]
    if start:
        body.insert(0, f"… lines 1-{start} omitted here (still in the log; use peek or cite ranges)")
    return "\n".join(body)


# ---------------------------------------------------------------------- tools


@mcp.tool()
async def preferred_tools(dir: str | None = None) -> str:
    """Which command to use for which job (HTTP, DB reads, auth checks, ...). Read it
    while planning, before open_session.

    Guidance, not enforcement: exec runs any command that passes its hard limits. The
    list merges the system file (~/.config/qa-bot/tools.md) with the nearest
    .qa-bot/tools.md at or above `dir`; a project ## section replaces the system one
    with the same heading.

    Args:
        dir: The planned session folder, or any folder in the project (default: the
            last session folder, else the server's working directory).
    """
    folder = dir or _last_dir or Path.cwd()
    return (await asyncio.to_thread(tools.load, folder)).describe()


@mcp.tool()
async def open_session(name: str, dir: str) -> str:
    """Start a recorded QA session: a tmux session running the user's own zsh.

    Args:
        name: Session name (also the tmux session name), e.g. "pay-123".
        dir: Report folder. session.log, the index, hash and qa-report.md go
            here; put test inputs in <dir>/inputs/. Use a fresh folder per session.

    Returns the session id, the `tmux attach` command for the human, the folder, and
    the preferred tools for this project.
    """
    global _session, _last_dir
    async with _lock:
        if _session is not None and not _session.closed:
            raise SessionError(f"Session '{_session.name}' is still open. Close it first.")
        _session = await asyncio.to_thread(Session.open, name, dir)
        _last_dir = _session.dir
        return (
            f"Session: {_session.name}\n"
            f"Watch / type along: {_session.attach_command}\n"
            f"Folder: {_session.dir}\n"
            "Recording started.\n\n"
            + (await asyncio.to_thread(tools.load, _session.dir)).describe()
        )


@mcp.tool(name="exec")
async def exec_command(command: str, timeout: float | None = None) -> str:
    """Type a command into the session's pane exactly as written and wait until it finishes.

    Use literal values (full URL, ids, payloads) so the command itself is the proof.
    Split long commands over lines with trailing '\\' continuations; multi-line SQL goes
    in one heredoc, e.g. psql ... <<'SQL' / ... / SQL. for/while/until loops are allowed,
    on one line or over several (do ... done); to wait for a state, prefer poll.

    Any executable may run; prefer the tools from preferred_tools. Refused (hard
    limits): $(...)/backticks, output redirection (except 2>&1 and >/dev/null), background jobs (&, nohup, disown),
    TRUNCATE/DROP, DELETE/UPDATE without WHERE, and anything while the command, the kube
    context or the pane's database env looks like production.

    Args:
        command: Shell command (aliases and functions from the user's zsh work).
        timeout: Seconds to wait for the command to finish (default 60).

    Returns the log entry id (#N, cite it in verdicts), exit code, duration and the
    numbered output lines. TIMEOUT returns the partial output (the command keeps
    running: peek or interrupt). NO_START means the command was typed but the shell
    hooks never reported it starting. BUSY means a command is running or the human is
    typing in the pane; nothing was typed.
    """
    async with _lock:
        session = _require_session()
        wait = _wait(timeout)
        result = await asyncio.to_thread(session.exec, command, wait)

    max_lines = int(_env_number("QA_BOT_EXEC_MAX_LINES", 300))
    if result.status == "BUSY":
        return (
            "BUSY: a command is running or the prompt is not empty (the human may be typing). "
            "Nothing was typed. Pane:\n" + "\n".join(result.output)
        )
    if result.status == "NO_START":
        return (
            f"NO_START after {wait:g}s: the command was typed but no start marker was seen "
            "(hooks broken or input not accepted). "
            "Pane:\n" + "\n".join(result.output)
        )
    header = f"#{result.entry_id}"
    if result.status == "TIMEOUT":
        header = f"TIMEOUT after {wait:g}s · {header} still running (peek / interrupt)"
    else:
        header += f" · exit {result.exit_code} · {result.duration_ms} ms"
    return f"{header}\n{_numbered(result.output, max_lines)}"


@mcp.tool()
async def poll(
    command: str,
    until: str,
    interval: float = 20,
    max_attempts: int = 10,
    timeout: float | None = None,
) -> str:
    """Run a command repeatedly until its output matches a regex: for async work
    (scheduler jobs, listeners, delayed processing) that reaches a state later.

    Each attempt is typed into the pane like exec, with the same hard limits and
    production check, and is recorded as its own log entry. The attempts form one poll
    group (P<n>) in the session index; citing any attempt's entry id (or
    {"poll": n}) in a verdict shows the group as one evidence block.

    Args:
        command: Shell command, same rules as exec. Typically a read: an HTTP GET or a
            select that shows the state.
        until: Python regex searched in the attempt's output (multi-line), e.g.
            '"status": "PROCESSED"'.
        interval: Seconds between attempts (default 20).
        max_attempts: Attempts before giving up (default 10).
        timeout: Seconds each attempt may run (default 60).

    Returns the outcome: matched, exhausted (no match after max_attempts), busy (the
    human is typing or a command is running; nothing more was typed), timeout (an
    attempt is still running: peek or interrupt), no_start (an attempt was typed but
    never reported starting), or interrupted (Ctrl-C). Also the
    number of attempts, elapsed time, the entry ids and the last attempt's numbered
    output.
    """
    try:
        re.compile(until, re.M)
    except re.error as exc:
        raise ValueError(f"'until' is not a valid regex: {exc}") from exc
    interval = max(1.0, min(float(interval), MAX_POLL_INTERVAL))
    max_attempts = max(1, min(int(max_attempts), MAX_POLL_ATTEMPTS))
    async with _lock:
        session = _require_session()
        wait = _wait(timeout)
        result = await asyncio.to_thread(session.poll, command, until, interval, max_attempts, wait)

    ids = result.entry_ids
    entries = ", ".join(f"#{i}" for i in ids) or "none"
    header = (
        f"P{result.poll_id} · {result.outcome} after {len(result.attempts)} attempt(s) "
        f"in {result.elapsed_s:.0f} s · entries {entries}"
    )
    if result.outcome == "busy":
        header += "\nBUSY: a command is running or the prompt is not empty (the human may be typing)."
    if not result.attempts:
        return header
    last = result.attempts[-1]
    max_lines = int(_env_number("QA_BOT_EXEC_MAX_LINES", 300))
    if last.status == "NO_START":
        return f"{header}\nLast attempt: typed, but no start marker seen. Pane:\n" + "\n".join(last.output)
    if last.status == "TIMEOUT":
        detail = f"#{last.entry_id} still running after {wait:g}s (peek / interrupt)"
    else:
        detail = f"#{last.entry_id} · exit {last.exit_code} · {last.duration_ms} ms"
    return f"{header}\nLast attempt {detail}\n{_numbered(last.output, max_lines)}"


@mcp.tool()
async def peek(lines: int = 40) -> str:
    """Show the pane's recent contents and whether it is busy, idle or being typed in."""
    session = _require_session()
    text = await asyncio.to_thread(session.peek, max(1, min(lines, 2000)))
    return f"State: {session.state()}\n{text}"


@mcp.tool()
async def interrupt() -> str:
    """Send Ctrl-C to the pane. Returns whether it is now busy or idle."""
    session = _require_session()
    state = await asyncio.to_thread(session.interrupt)
    return f"Sent Ctrl-C. State: {state}"


@mcp.tool()
async def close_session(keep_tmux: bool = False) -> str:
    """Stop recording; write session.index.json and session.log.sha256.

    Args:
        keep_tmux: Leave the tmux session running (it is no longer recorded).
    """
    async with _lock:
        session = _require_session()
        log_hash, count = await asyncio.to_thread(session.close, not keep_tmux)
        return (
            f"Log: {session.log_path}\n"
            f"Entries: {count}\n"
            f"sha256: {log_hash}"
        )


@mcp.tool()
async def render_report(
    verdicts: dict | None = None,
    dir: str | None = None,
    evidence: Literal["text", "image", "both"] = "text",
) -> str:
    """Validate verdicts against the closed log and render <dir>/qa-report.md.

    Args:
        verdicts: The verdicts object. If given it is written to <dir>/verdicts.json;
            otherwise <dir>/verdicts.json is read. Shape:
            {"ticket", "env", "build", "tests": [{"id", "goal", "input", "expected",
            "verdict": "pass|fail|skipped", "actual", "note",
            "evidence": [2, {"entry": 4, "lines": "1-3"}, {"poll": 1}]}]}
            An entry id that belongs to a poll group (or {"poll": n}) shows the whole
            group as one block: first and last attempt. Cite {"entry": id, "lines": ...}
            to show just that attempt.
        dir: Session folder (default: the last session opened).
        evidence: How cited log lines appear in the report. "text": code blocks.
            "image": PNGs rendered from the same lines into <dir>/evidence/ (needs
            freeze), for pasting into Jira, Confluence or a PR. "both": the image,
            then the text in a collapsed <details>.

    Fails if session.log or the index changed after close_session. Returns the report
    path and warnings. Verdict warnings: fix the verdicts and render again. Warnings
    starting "Log anomaly" or "Manual entries no verdict cites" are about the log
    itself and can't be fixed through verdicts; report them to the user.
    """
    directory = Path(dir).expanduser() if dir else _last_dir
    if directory is None:
        raise SessionError("No session folder known; pass dir.")
    if _session is not None and not _session.closed and _session.dir == directory.resolve():
        raise SessionError("Close the session before rendering the report.")
    path, warnings = await asyncio.to_thread(report.render_report, directory, verdicts, evidence)
    if not warnings:
        return f"Report: {path}\nNo validation warnings."
    return f"Report: {path}\nValidation warnings ({len(warnings)}):\n" + "\n".join(f"- {w}" for w in warnings)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()


__all__ = ["Refused", "mcp", "main"]
