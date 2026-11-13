"""Helpers that build session.log bytes the way the zsh hooks + tty produce them.

Marker bytes come from the real encoder in ``qa_bot.hooks``; this file only adds what
the tty does around them (echo, onlcr, zsh's prompt).
"""

from qa_bot import hooks

PROMPT_SP = b"\x1b[1m\x1b[7m%\x1b[27m\x1b[1m\x1b[0m" + b" " * 119 + b"\r \r"
PROMPT = b"\r\x1b[0m\x1b[27m\x1b[24m\x1b[J% \x1b[K\x1b[?2004h"


def start(eid, cmd, t=1_700_000_000.0):
    return hooks.encode_start(eid, t, cmd).replace(b"\n", b"\r\n")  # tty onlcr


def end(eid, code=0, t=1_700_000_000.3):
    return hooks.encode_end(eid, code, t)


def entry(eid, cmd, output: bytes, code=0, t=1_700_000_000.0, dur=0.312):
    echo = cmd.encode().replace(b"\n", b"\r\n") + b"\x1b[?2004l\r\r\n"
    return echo + start(eid, cmd, t) + output + PROMPT_SP + end(eid, code, t + dur) + PROMPT
