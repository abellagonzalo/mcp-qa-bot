---
name: manual-qa
description: Manually test a change on dev or staging the way a person would — real endpoints, real database, real logs — with a recorded terminal session and a rendered QA report as proof. Use when the user asks to QA, manually test, or verify a ticket/PR on dev or staging. Requires the qa-bot MCP server (open_session, exec, peek, interrupt, close_session, render_report).
---

# Manual QA

Gray-box testing: read the code to know what to test and how to trigger it, then
exercise the running system from outside, through the qa-bot tools. Every command runs
in a tmux pane the user can watch and type in; the tool records it and renders the
report. You plan, run and judge. You never write evidence yourself.

**Production is never a target.** Only dev or staging. The tool refuses production
contexts too, but never try to work around a refusal.

## 1. Understand (read-only)

- Read the ticket or PR, the diff, and the service's code: endpoints, request
  shapes, feature flags, DB tables, and which process does any async work. The repo is
  the source of truth for endpoints.
- Read the target env's config and check which build it is running, e.g.
  `kubectl get deploy <svc> -o jsonpath='{.spec.template.spec.containers[0].image}'`.
  If the change isn't deployed there, stop and tell the user.
- Read-only commands only in this phase (`kubectl get`, `select`, `curl` GETs).

## 2. Plan

Pick **3–5 tests**: the happy path, the riskiest edge case, and a regression check on
the nearest existing behaviour. For each test, write:

- **Goal**: one line.
- **Input**: payload or file (save it as `<dir>/inputs/<test>.json`).
- **Expected outcome**: concrete and checkable (status codes, row counts, log lines).
- **Exact commands** it will run, with literal values: full URL, ids, payloads. No
  `$VARS` the reader can't see, so a reviewer can read the command and rerun it. HTTP is
  plain `curl`; SQL is `psql … <<'SQL' … SQL` or `psql -c '…'`.
- **Pretty-print JSON** so the output (and the report) is readable:
  - Body only: `curl -s … | jq .`
  - Body + status code: `curl -s -w '\n%{http_code}\n' … | jq -Rr 'fromjson? // .'`.
    Each JSON line is pretty-printed, and the status line and non-JSON bodies (HTML
    error pages) pass through unchanged.
  - Status only: `curl -s -o /dev/null -w '%{http_code}' …` (nothing to pretty-print).
  - JSON from `kubectl`: `-o json | jq '<filter>'` and pick only the fields that prove
    the result.
  - Keep the output short enough to cite: filter with `jq` instead of dumping large
    documents.
- **Write commands over multiple lines** so they read well in the pane and the report.
  Put one flag, header or argument per line, end each line with ` \`, and indent the
  continuation lines by two spaces. A pipe goes at the start of its own line. Short
  commands (`curl -s <url> | jq .`) can stay on one line.

  ```bash
  curl -s -w '\n%{http_code}\n' \
    -X POST http://localhost:8099/v1/transfers \
    -H 'Content-Type: application/json' \
    -H 'Idempotency-Key: qa-pay123-t1-20261112' \
    -d '{"from":"acc_qa_1","to":"acc_qa_2","amount":1500}' \
    | jq -Rr 'fromjson? // .'
  ```
- **Write SQL over multiple lines** in a heredoc, never in a long `-c '…'`. Put one
  clause per line (`select`, `from`, `where`, `and`, `order by`) and list columns
  explicitly.

  ```bash
  psql postgresql://qa:qa@localhost:55432/payments <<'SQL'
  select id, amount, idempotency_key, state
    from transfers
   where idempotency_key = 'qa-pay123-t1-20261112';
  SQL
  ```
- Anything else multi-line (a newline inside quotes, two heredocs) is refused by
  `exec`. Only ` \` continuations and one heredoc are allowed.
- Created entities get **unique ids / idempotency keys** (e.g. `qa-pay123-t1-<date>`)
  so reruns don't collide.

## 3. Danger review

Label every command **read-only** or **dangerous**. For each dangerous one, say what it
changes (rows, messages, external calls) and whether and how it can be undone.

## 4. Approval gate

**Stop.** Show the plan and the danger review, then wait for the user to explicitly
approve it. Don't run anything except the read-only commands from step 1 before that.
If you need to change, add or reorder commands after approval, show the change and get
approval again. The user's own commands in the pane need no approval.

## 5. Execute

1. `open_session(name, dir)`: use e.g. `name="pay-123"` and a fresh folder like
   `./qa/pay-123-<date>`. Give the user the `tmux attach` command it returns, then
   **wait** until they say they've attached (or that they don't want to watch) before
   the first `exec`. Commands finish in milliseconds, so without this pause the whole
   run is over before anyone can attach.
2. Start with a context check, e.g. `kubectl config current-context`.
3. Run each approved command with `exec`. Note the entry id (`#N`) of every result;
   verdicts cite them. Line numbers in `exec` output are the ones you cite.
4. If `exec` returns:
   - **BUSY**: the user is typing or a command is running. Don't type over them. Wait,
     `peek`, or ask.
   - **TIMEOUT**: `peek` to see progress; `interrupt` if it hangs. Never retry a
     dangerous command blindly.
   - A refusal (allowlist, production, forbidden SQL): don't try to get around it.
     Report it and adjust the plan with the user.
5. If the user runs commands in the pane, they are recorded as manual entries. You may
   cite them.
6. `close_session(keep_tmux=true)` when done, so the user can still scroll the pane
   (recording has stopped). Tell them to end it with `tmux kill-session -t <name>`.

## 6. Verdicts

Write the verdicts for every planned test: `pass`, `fail` or `skipped` (with the
reason in `note`). Each non-skipped test cites at least one log entry. Use line ranges
for long output.

```json
{
  "ticket": "PAY-123",
  "env": "staging",
  "build": "payments@a1b2c3d",
  "tests": [{
    "id": "T1",
    "goal": "Duplicate idempotency key is rejected",
    "input": "inputs/t1.json",
    "expected": "1st POST 201, 2nd POST 409, one row in transfers",
    "verdict": "fail",
    "actual": "2nd POST returned 201; two rows created",
    "evidence": [2, {"entry": 4, "lines": "1-3"}, 5],
    "note": "IdempotencyFilter is not registered on /v1/transfers (TransferController.kt:42)"
  }]
}
```

- `evidence` items are an entry id (whole entry) or `{"entry": id, "lines": "a-b"}`.
- A failure states actual vs expected. It may point to a bug in the code or in the
  ticket; say which you think it is in `note`.
- Judge from the text output `exec` returns.

## 7. Report

Call `render_report(verdicts)`. If it returns validation warnings, fix the verdicts
and render again until it returns none.

Evidence is text by default. When the user wants images (to paste into Jira,
Confluence or a PR), pass `evidence="image"`, or `"both"` to keep the text in a
collapsed block. Images are drawn from the cited lines, so cite tight line ranges: a
warning flags any image over 60 lines. Then reply in chat with the report path, the
pass/fail summary, any side effects left in the env (created rows, messages), and
anything that needs a follow-up.
