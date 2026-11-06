# mcp-qa-bot

An MCP server (Python, FastMCP) that automates manual QA steps that need terminal
commands plus a screenshot as proof of work.

It exposes one tool, **`execute_and_screenshot`**, which:

1. checks the command against an allowlist (default `kubectl`, `curl`, `psql`),
2. brings iTerm2 to the foreground with AppleScript,
3. optionally runs `clear`, then types the command into the **current iTerm2 session**,
   which keeps kube context, env vars and open psql sessions,
4. waits a configurable number of seconds for output,
5. captures **only the iTerm2 window** with `screencapture -l <windowId>`,
6. returns the PNG path, the visible terminal text and, optionally, the image inline.

## Requirements

- macOS with iTerm2
- Python ≥ 3.10 (Homebrew: `brew install python`)
- Pinned to `mcp<2`: in the 2.x SDK, FastMCP was renamed to `MCPServer`.

## Setup

```bash
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # allowlist unit tests
```

### macOS permissions (first run)

- **Automation**: allow the host app (Claude Desktop, Terminal, …) to control iTerm2.
- **Screen Recording**: System Settings → Privacy & Security → Screen Recording, enable
  the host app. Without it, screenshots show only the desktop wallpaper.

## Configuration

| Env var                   | Default             | Meaning                                    |
|---------------------------|---------------------|--------------------------------------------|
| `QA_BOT_ALLOWED_COMMANDS` | `kubectl,curl,psql` | Comma-separated allowed executables        |
| `QA_BOT_SCREENSHOT_DIR`   | `~/qa-screenshots`  | Where PNGs are written                     |
| `QA_BOT_DEFAULT_WAIT`     | `3`                 | Seconds to wait before capturing (max 300) |

Allowlist rules: pipes and `&&`/`;` chains are allowed if **every** segment starts with
an allowed executable, so for `kubectl get pods | grep Running` add `grep` to the list.
Command substitution (`$(…)`, backticks), output redirection (`>`) and multi-line
commands are always rejected.

## Tool parameters

| Param           | Default  | Notes                                                   |
|-----------------|----------|---------------------------------------------------------|
| `command`       | required | Command to run                                          |
| `wait_seconds`  | env/3    | Increase for slow commands                              |
| `label`         | –        | Used in the filename, e.g. `pods-ready`                 |
| `clear_screen`  | `true`   | Set `false` when inside an interactive prompt like psql |
| `include_image` | `false`  | Also return the PNG inline to the LLM                   |
| `output_lines`  | `60`     | Trailing lines of terminal text to return               |

## Running / testing

Standalone (stdio):

```bash
.venv/bin/python qa_server.py
```

MCP Inspector:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python qa_server.py
```

## Claude Desktop / Claude Code integration

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "qa-bot": {
      "command": "/Users/gonzalo.abella/github/abellagonzalo/mcp-qa-bot/.venv/bin/python",
      "args": ["/Users/gonzalo.abella/github/abellagonzalo/mcp-qa-bot/qa_server.py"],
      "env": {
        "QA_BOT_ALLOWED_COMMANDS": "kubectl,curl,psql,grep,jq",
        "QA_BOT_SCREENSHOT_DIR": "/Users/gonzalo.abella/qa-screenshots"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add qa-bot -- /Users/gonzalo.abella/github/abellagonzalo/mcp-qa-bot/.venv/bin/python \
  /Users/gonzalo.abella/github/abellagonzalo/mcp-qa-bot/qa_server.py
```

> ⚠️ The tool types into the *active* iTerm2 session. Don't run it from a Claude Code
> session that is itself running in iTerm2's active tab, or the command will be typed
> into Claude Code's prompt. Keep a separate iTerm2 window focused for QA.
