Preferred tools for manual QA. Guidance for the agent, not enforced by exec.
Copy to ~/.config/qa-bot/tools.md (system) or <repo>/.qa-bot/tools.md (project).
A project "## section" replaces the system section with the same heading.

## HTTP to services
`curl -s` with the full URL; pretty-print JSON with `| jq .`.

## DB reads
`psql <connection url> <<'SQL' … SQL` with a read-only user.

## Cluster and context checks
`kubectl config current-context`, `kubectl get deploy <svc> -o jsonpath='{.spec.template.spec.containers[0].image}'`.
