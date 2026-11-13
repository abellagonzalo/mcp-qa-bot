Preferred tools for manual QA. Guidance for the agent, not enforced by exec.
Copy to ~/.config/qa-bot/tools.md (system) or <repo>/.qa-bot/tools.md (project).
A project "## section" replaces the system section with the same heading.

## HTTP to services
`curl -s` with the full URL; pretty-print JSON with `| jq .`.

## DB reads
`psql <connection url> <<'SQL' … SQL` with a read-only user.

## Cluster and context checks
`kubectl config current-context`, `kubectl get deploy <svc> -o jsonpath='{.spec.template.spec.containers[0].image}'`.

## Tickets
Read the ticket with your tracker's CLI or MCP tool, e.g. `<tracker> issue view <id>`.

## Pull requests
`gh pr view <number> --comments` for the description; `gh pr diff <number>` for the diff.
Download embedded images with `gh` or the tracker tool, then read them.

## Deploys
How to deploy a branch or commit to dev/staging, e.g. `gh workflow run deploy.yml -f ref=<sha> -f env=staging`.
Always a dangerous command: it goes through the plan's danger review.
