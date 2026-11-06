#!/usr/bin/env python3
"""mcp-qa-bot: MCP server that runs QA commands in iTerm2 and screenshots the result.

Exposes one tool, ``execute_and_screenshot``, which:
  1. validates the command against an allowlist,
  2. brings iTerm2 to the foreground (AppleScript),
  3. optionally clears the current session, then types the command into it,
  4. waits for output to populate,
  5. captures only the iTerm2 window with macOS ``screencapture -l <window id>``.

Configuration (environment variables):
  QA_BOT_ALLOWED_COMMANDS  comma-separated executables allowed (default: kubectl,curl,psql)
  QA_BOT_SCREENSHOT_DIR    where PNGs are written (default: ~/qa-screenshots)
  QA_BOT_DEFAULT_WAIT      default seconds to wait for output (default: 3)
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

DEFAULT_ALLOWED = ("kubectl", "curl", "psql")
MAX_WAIT_SECONDS = 300.0
CONTROL_OPERATORS = {"|", "||", "&&", ";", "&", "|&", ";;"}
REDIRECT_TOKENS = {">", ">>", ">&", "&>", "&>>", ">|"}
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

mcp = FastMCP("mcp-qa-bot")


# --------------------------------------------------------------------------- config


def allowed_commands() -> set[str]:
    raw = os.environ.get("QA_BOT_ALLOWED_COMMANDS")
    if not raw:
        return set(DEFAULT_ALLOWED)
    return {c.strip() for c in raw.split(",") if c.strip()}


def screenshot_dir() -> Path:
    path = Path(os.environ.get("QA_BOT_SCREENSHOT_DIR", "~/qa-screenshots")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_wait() -> float:
    try:
        return float(os.environ.get("QA_BOT_DEFAULT_WAIT", "3"))
    except ValueError:
        return 3.0


# ------------------------------------------------------------------------ allowlist


class CommandNotAllowed(ValueError):
    pass


def validate_command(command: str, allowed: set[str]) -> None:
    """Raise CommandNotAllowed unless every command in the pipeline is allowlisted.

    Pipes and ``&&``/``;`` chains are permitted as long as each segment starts with an
    allowed executable. Command substitution, newlines and output redirection are
    rejected outright because they can run or write things the allowlist can't see.
    """
    if not command.strip():
        raise CommandNotAllowed("Command is empty.")
    if "\n" in command or "\r" in command:
        raise CommandNotAllowed("Multi-line commands are not allowed.")
    if "$(" in command or "`" in command or "<(" in command or ">(" in command:
        raise CommandNotAllowed("Command/process substitution is not allowed.")

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:  # unbalanced quotes etc.
        raise CommandNotAllowed(f"Could not parse command: {exc}") from exc

    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in CONTROL_OPERATORS:
            segments.append([])
        elif tok in REDIRECT_TOKENS:
            raise CommandNotAllowed("Output redirection is not allowed.")
        else:
            segments[-1].append(tok)

    for seg in segments:
        # Skip leading VAR=value assignments, e.g. `KUBECONFIG=x kubectl ...`.
        i = 0
        while i < len(seg) and ENV_ASSIGNMENT.match(seg[i]):
            i += 1
        words = seg[i:]
        if not words:
            raise CommandNotAllowed("Empty command segment (dangling operator?).")
        exe = os.path.basename(words[0])
        if exe not in allowed:
            raise CommandNotAllowed(
                f"'{exe}' is not in the allowlist ({', '.join(sorted(allowed))}). "
                "Extend it with QA_BOT_ALLOWED_COMMANDS."
            )


# ------------------------------------------------------------------------ macOS glue

# The command is passed as argv to avoid any AppleScript string escaping issues.
_RUN_IN_ITERM = """
on run argv
    set theCommand to item 1 of argv
    set shouldClear to (item 2 of argv) is "1"
    tell application "iTerm2"
        activate
        if (count of windows) is 0 then
            create window with default profile
            delay 0.5
        end if
        tell current session of current window
            if shouldClear then
                write text "clear"
                delay 0.3
            end if
            write text theCommand
        end tell
        return id of current window
    end tell
end run
"""

_READ_SESSION_TEXT = """
tell application "iTerm2"
    return contents of current session of current window
end tell
"""


def _osascript(script: str, *args: str) -> str:
    result = subprocess.run(
        ["osascript", "-", *args],
        input=script,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript failed: {result.stderr.strip()}")
    return result.stdout.strip()


def run_in_iterm(command: str, clear: bool) -> str:
    """Type ``command`` into the active iTerm2 session; return the window id."""
    return _osascript(_RUN_IN_ITERM, command, "1" if clear else "0")


def read_session_text(max_lines: int) -> str:
    try:
        text = _osascript(_READ_SESSION_TEXT)
    except RuntimeError:
        return ""
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-max_lines:])


def capture_window(window_id: str, dest: Path) -> None:
    # -l: capture a specific window, -o: no window shadow, -x: no shutter sound
    result = subprocess.run(
        ["screencapture", "-x", "-o", "-l", window_id, str(dest)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError(
            f"screencapture failed (window {window_id}): {result.stderr.strip() or 'no file written'}. "
            "Make sure the app running this server has Screen Recording permission."
        )


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:60] or "qa"


# ----------------------------------------------------------------------------- tool


@mcp.tool()
async def execute_and_screenshot(
    command: str,
    wait_seconds: float | None = None,
    label: str | None = None,
    clear_screen: bool = True,
    include_image: bool = False,
    output_lines: int = 60,
) -> list:
    """Run a QA command (kubectl, curl, psql, ...) in iTerm2 and screenshot the window as proof.

    The command is typed into the active iTerm2 session, so kube context, env vars and
    open psql sessions are preserved between calls.

    Args:
        command: Shell command to run. Every executable in it must be allowlisted.
        wait_seconds: Seconds to wait for output before capturing (default from
            QA_BOT_DEFAULT_WAIT, 3s). Increase for slow commands.
        label: Short description used in the screenshot filename, e.g. "pods-ready".
        clear_screen: Run `clear` first so the screenshot shows only this command.
            Set false when inside an interactive prompt such as psql.
        include_image: Also return the PNG inline so it can be viewed directly.
        output_lines: How many trailing lines of terminal text to return.

    Returns the screenshot path and the visible terminal text (plus the image if requested).
    """
    validate_command(command, allowed_commands())

    wait = default_wait() if wait_seconds is None else wait_seconds
    wait = max(0.0, min(wait, MAX_WAIT_SECONDS))

    window_id = await asyncio.to_thread(run_in_iterm, command, clear_screen)
    await asyncio.sleep(wait)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = screenshot_dir() / f"{timestamp}-{_slug(label or command)}.png"
    await asyncio.to_thread(capture_window, window_id, dest)

    terminal_text = await asyncio.to_thread(read_session_text, max(1, output_lines))

    summary = (
        f"Screenshot saved: {dest}\n"
        f"Command: {command}\n"
        f"Waited: {wait:g}s\n\n"
        f"--- terminal output (last {output_lines} lines) ---\n{terminal_text}"
    )
    content: list = [summary]
    if include_image:
        content.append(Image(path=str(dest)))
    return content


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
