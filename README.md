# mcp-qa-bot

The execution layer for agent-driven manual QA on **dev or staging**.

An MCP server (Python, FastMCP) that runs shell commands in a **tmux pane a human can
watch and type in**, records everything that runs there, and renders a QA report from
verdicts it is given. It knows nothing about QA. The workflow (understand, plan,
danger review, approval, verdicts) lives in the [`manual-qa` skill](skills/manual-qa/SKILL.md).

| | Execution tool (this server) | QA skill (`skills/manual-qa`) |
|---|---|---|
| Owns | tmux session, running commands, detecting when they finish, the session log, rendering the report (as text or images), hard safety limits | understanding the change, planning tests, danger review, approval gate, verdicts |
| Guarantees | the log is complete and tamper-evident; report evidence is copied verbatim from the log; forbidden commands never run | the plan is approved before anything runs; every verdict cites log entries |

## Tools

| Tool | Does | Returns |
|---|---|---|
| `preferred_tools(dir?)` | Reads the [preferred tools](#preferred-tools) for the project, for planning | merged Markdown list |
| `open_session(name, dir)` | Creates the session folder and tmux session (the user's own zsh), loads the hooks, starts recording | session id, `tmux attach` command, folder, preferred tools |
| `exec(command, timeout?)` | Types the command into the pane exactly as written and waits for the hooks to signal it finished | entry id `#N`, exit code, duration, numbered output; or `TIMEOUT` / `BUSY` |
| `peek(lines?)` | Shows the pane's recent contents | text + `idle` / `running` / `typing` |
| `interrupt()` | Sends Ctrl-C | state |
| `close_session(keep_tmux?)` | Stops recording, writes the exec index and the hashes | log path, entry count, sha256 |
| `render_report(verdicts?, dir?, evidence?)` | Checks integrity, validates verdicts against the closed log, renders `qa-report.md` with evidence as `text`, `image` or `both` | report path, warnings (verdict problems and log anomalies) |

## How it works

- `open_session` starts `zsh -l` in a detached tmux session, so the user's `.zshrc`,
  aliases and plugins load. It types one line, `source qa_bot/hooks.zsh …`, and waits
  for the hooks' first `tmux wait-for` signal. Only then does `tmux pipe-pane` start
  streaming the pane into `session.log`. No sleeps.
- [`qa_bot/hooks.zsh`](qa_bot/hooks.zsh) wraps every command line, from the agent or a
  human, in invisible OSC markers that travel through the pty with the output:
  `ESC]777;qa;start;<id>;<epoch>;<command>BEL` … `ESC]777;qa;end;<id>;<exit>;<epoch>BEL`.
  Ids come from a counter in the shell, so agent and human commands share one sequence.
  `precmd` signals `tmux wait-for`, which is how `exec` knows a command finished.
- `exec` pastes the command with bracketed paste, so `;` and multi-line heredocs
  arrive exactly as written, then presses Enter. It returns `BUSY` without typing if a
  command is running or the prompt line isn't empty (the human is typing).
- `render_report` parses the log and classifies each marker pair against
  `session.index.json`: 🤖 agent (matches an `exec` call), 👤 manual (top level, not in
  the index), ⚠️ forged (inside another entry's output), ⚠️ missing (`exec` call with no
  markers). Entry output is cleaned deterministically: escape sequences are stripped
  and `\r` overwrites are replayed. Lines are numbered from 1 per entry.
- The report has a summary table, then one section per test with its cited evidence.
  There is no timeline of every entry. Anything wrong with the log instead becomes a
  warning, shown in a `[!WARNING]` box at the top and returned by `render_report`:
  each forged or stray marker, each `exec` call with no markers, each flagged entry
  (command mismatch, unterminated) no verdict cites, and the list of manual entries no
  verdict cites (they may have changed state). A clean run has no warning box.
- Evidence is the cited lines (`$ command` + output, or just the cited range). With
  `evidence="image"` the same lines are drawn as PNGs with `freeze` into `evidence/`,
  for pasting into Jira, Confluence or a PR; `"both"` adds the text in a collapsed
  `<details>`. Images are not pane screenshots: they are derived from the hashed log,
  so they can always be regenerated and never disagree with it. Each render rebuilds
  `evidence/`.

## Hard limits (enforced on every `exec`)

- Any executable may run. There is no allowlist: the approval gate in the skill and the
  production guard are the safety boundary. Which tools to use is guidance (see
  [Preferred tools](#preferred-tools)).
- Refused: `$(…)`, backticks, process substitution, output redirection (`2>&1` and
  `>/dev/null` are fine), background jobs (`&`, `nohup`, `disown`), and multi-line input
  other than `\` line continuations and a single heredoc.
- Refused SQL, anywhere in the command: `TRUNCATE`, `DROP …`, and `DELETE`/`UPDATE`
  without `WHERE`.
- Production guard, checked before every `exec` because a human can switch context
  mid-session. It refuses if the command, the pane's `KUBECONFIG` context, cluster or
  server, or the pane's `PGHOST`, `PGDATABASE`, `PGSERVICE` or `DATABASE_URL` matches
  `QA_BOT_PROD_PATTERN`.

Commands the human types are not restricted. They are recorded and shown in the
report as manual entries.

## Preferred tools

A list of which command to use for which job, e.g. "HTTP to in-cluster services:
`kubectl curl`, not plain `curl`". It is guidance for the agent, not enforcement.
`preferred_tools` returns it for planning and `open_session` returns it again at
session start.

| Scope | File |
|---|---|
| System | `$XDG_CONFIG_HOME/qa-bot/tools.md` (default `~/.config/qa-bot/tools.md`) |
| Project | the nearest `.qa-bot/tools.md` in the session folder or a parent folder (usually the repo root) |

Both are free-form Markdown, one `## heading` per job. The project file extends the
system file: a project section replaces the system section with the same heading
(case-insensitive), and other project sections are added. Text before the first `##`
is kept from both. See [`examples/tools.md`](examples/tools.md).

## Session folder

```
<dir>/
  qa-report.md          # rendered by render_report
  verdicts.json         # written by the skill (or by render_report from its argument)
  session.log           # raw tmux transcript, markers included
  session.index.json    # exec calls: id, command, timestamp
  session.log.sha256    # sha256 of log + index (`shasum -a 256 -c session.log.sha256`)
  evidence/             # images of cited lines (<test>-<k>.png), only with evidence=image|both
  inputs/               # test inputs (payloads, CSVs)
```

## Requirements

- macOS or Linux with **tmux ≥ 3.0** and **zsh ≥ 5.3**
- Python ≥ 3.10; pinned to `mcp<2` (FastMCP was renamed in the 2.x SDK)
- Optional: [`freeze`](https://github.com/charmbracelet/freeze) for image evidence
  (`brew install charmbracelet/tap/freeze`)

No macOS Automation or Screen Recording permissions are needed.

## Setup

```bash
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest   # unit tests + a tmux/zsh integration test on a private socket
```

## Configuration

| Env var                   | Default | Meaning |
|---------------------------|---------|---------|
| `QA_BOT_PROD_PATTERN`     | `(?<![a-z0-9])(prod\|production\|prd)(?![a-z])` | Case-insensitive regex for production names |
| `QA_BOT_DEFAULT_TIMEOUT`  | `60`    | Seconds `exec` waits for a command (max 1800) |
| `QA_BOT_EXEC_MAX_LINES`   | `300`   | Trailing output lines `exec` returns to the agent |
| `QA_BOT_REPORT_MAX_LINES` | `200`   | Lines shown when an entry is cited without a line range (a warning asks for a range) |
| `QA_BOT_IMAGE_MAX_LINES`  | `60`    | Lines per evidence image before `render_report` warns (cite a narrower range) |
| `QA_BOT_TMUX_SOCKET`      | –       | tmux socket name (`-L`); default is the user's normal tmux server |

## Claude Code integration

```bash
claude mcp add qa-bot -- /path/to/mcp-qa-bot/.venv/bin/python /path/to/mcp-qa-bot/qa_server.py
ln -s /path/to/mcp-qa-bot/skills/manual-qa ~/.claude/skills/manual-qa
```

Then ask Claude to "QA PAY-123 on staging" and attach to the session it opens with the
`tmux attach -t …` command it prints.

MCP Inspector:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python qa_server.py
```

## Not in scope yet

- Hiding secrets in logs and reports.
- Browser-driven testing.
