"""Hard limits on what ``exec`` may type into the pane.

These checks run in code on every agent command, whatever the model decides. Any
executable may run; what is refused is shell constructs that hide or detach work
(substitution, output redirection, background jobs), unexpected multi-line input,
and destructive SQL. Which tools the agent *should* use is guidance, not enforcement
(see ``tools.py``). Manual commands typed by the human are not restricted, only
recorded.
"""

from __future__ import annotations

import os
import re
import shlex

CONTROL_OPERATORS = {"|", "||", "&&", ";", "|&", ";;"}
BACKGROUND_OPERATOR = "&"
BACKGROUND_COMMANDS = {"nohup", "disown", "setsid", "bg"}
OUTPUT_REDIRECTS = {">", ">>", ">&", "&>", "&>>", ">|"}
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# The first logical line of a multi-line command must open exactly one heredoc: <<TAG, <<-TAG, <<'TAG', <<"TAG"
HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1\s*")

# SQL that is refused outright, wherever it appears in the command (heredoc body,
# `-c '...'`, behind an alias). Refusing a false positive is the safe failure mode.
_SQL_ALWAYS_FORBIDDEN = [
    (re.compile(r"\btruncate\b", re.I), "TRUNCATE"),
    (
        re.compile(
            r"\bdrop\s+(table|database|schema|index|view|materialized|sequence|function|"
            r"procedure|trigger|type|extension|role|user|owned|column|constraint)\b",
            re.I,
        ),
        "DROP",
    ),
]
_SQL_DELETE = re.compile(r"\bdelete\s+from\b", re.I)
_SQL_UPDATE = re.compile(r"\bupdate\s+[\w.\"]+\s+set\b", re.I)
_SQL_WHERE = re.compile(r"\bwhere\b", re.I)


class CommandNotAllowed(ValueError):
    pass


def _continues(line: str) -> bool:
    """True if the line ends with an unescaped backslash (shell line continuation)."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _split_heredoc(command: str) -> tuple[str, str]:
    """Return (shell line, heredoc body). Commands without a heredoc have an empty body.

    Leading lines ending in `\\` are joined into one logical shell line first, so
    commands may be split over several lines for readability.
    """
    physical = command.split("\n")
    logical = physical[0]
    i = 1
    while _continues(logical) and i < len(physical):
        logical = logical[:-1] + " " + physical[i]
        i += 1
    if _continues(logical):
        raise CommandNotAllowed("The command ends with a dangling line continuation '\\'.")
    lines = [logical, *physical[i:]]
    if len(lines) == 1:
        if HEREDOC_OPEN.search(logical):
            raise CommandNotAllowed("A heredoc needs its body and terminator on the following lines.")
        return logical, ""

    first, body, last = lines[0], lines[1:-1], lines[-1]
    opens = list(HEREDOC_OPEN.finditer(first))
    if len(opens) != 1:
        raise CommandNotAllowed(
            "Multi-line commands are only allowed with '\\' line continuations and/or a "
            "single heredoc, e.g. \"psql ... <<'SQL'\" / ... / \"SQL\"."
        )
    tag = opens[0].group(2)
    if last.strip() != tag:
        raise CommandNotAllowed(f"The last line of the heredoc must be its terminator '{tag}'.")
    if any(line.strip() == tag for line in body):
        raise CommandNotAllowed(f"Heredoc terminator '{tag}' appears before the last line.")
    shell_line = HEREDOC_OPEN.sub(" ", first)
    return shell_line, "\n".join(body)


def _harmless_redirect(op: str, target: str) -> bool:
    """`2>&1` (fd duplication) and `>/dev/null` write no files."""
    if op == ">&":
        return target.isdigit()
    if op in {">", ">>", "&>"}:
        return target == "/dev/null"
    return False


def _check_sql(text: str) -> None:
    for statement in text.split(";"):
        for pattern, name in _SQL_ALWAYS_FORBIDDEN:
            if pattern.search(statement):
                raise CommandNotAllowed(f"{name} statements are forbidden.")
        if _SQL_DELETE.search(statement) and not _SQL_WHERE.search(statement):
            raise CommandNotAllowed("DELETE without WHERE is forbidden.")
        if _SQL_UPDATE.search(statement) and not _SQL_WHERE.search(statement):
            raise CommandNotAllowed("UPDATE without WHERE is forbidden.")


def validate_command(command: str) -> None:
    """Raise CommandNotAllowed unless the command passes every hard limit.

    - Command/process substitution, output redirection and background jobs (`&`,
      `nohup`, `disown`) are refused.
    - Multi-line commands are only allowed as '\\' line continuations and/or a single
      heredoc (e.g. SQL for psql).
    - TRUNCATE, DROP, and DELETE/UPDATE without WHERE are refused.
    """
    if not command.strip():
        raise CommandNotAllowed("Command is empty.")
    if "\r" in command:
        raise CommandNotAllowed("Carriage returns are not allowed in commands.")
    if "$(" in command or "`" in command or "<(" in command or ">(" in command:
        raise CommandNotAllowed("Command/process substitution is not allowed.")

    shell_line, heredoc_body = _split_heredoc(command)

    lexer = shlex.shlex(shell_line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:  # unbalanced quotes etc.
        raise CommandNotAllowed(f"Could not parse command: {exc}") from exc

    segments: list[list[str]] = [[]]
    for i, tok in enumerate(tokens):
        if tok == BACKGROUND_OPERATOR:
            raise CommandNotAllowed("Background jobs ('&') are not allowed.")
        if tok in CONTROL_OPERATORS:
            segments.append([])
        elif tok in OUTPUT_REDIRECTS:
            if not _harmless_redirect(tok, tokens[i + 1] if i + 1 < len(tokens) else ""):
                raise CommandNotAllowed(
                    "Output redirection is not allowed (except '2>&1' and '>/dev/null')."
                )
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
        if exe in BACKGROUND_COMMANDS:
            raise CommandNotAllowed(f"'{exe}' is not allowed (background jobs).")

    _check_sql(shell_line + "\n" + heredoc_body)
