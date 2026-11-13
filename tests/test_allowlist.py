import pytest

from qa_bot.allowlist import CommandNotAllowed, validate_command

ALLOWED = {"kubectl", "curl", "psql", "grep", "jq"}


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl get pods -n payments",
        "curl -s 'https://example.com/health'",
        "psql -h localhost -c 'select 1;'",
        "kubectl get pods | grep Running",
        "KUBECONFIG=/tmp/kc kubectl get ns",
        "kubectl get ns && kubectl get pods",
        "/usr/local/bin/kubectl version",
        """curl -d '{"a": ">"}' http://x""",
        "curl -s 'https://x/a?b=1&c=2' | jq .",
        "kubectl logs deploy/x 2>&1 | grep ERROR",
        "curl -s -o /dev/null -w '%{http_code}' https://x",
        "kubectl get pods 2>/dev/null",
        "psql -h db.staging -d payments <<'SQL'\nselect count(*) from transfers where id = 'x';\nSQL",
        "psql <<SQL\nupdate transfers set state = 'X' where id = 1;\nSQL",
        "psql -c \"delete from transfers where id = 'abc'\"",
        # line continuations
        "curl -s \\\n  -X POST \\\n  -H 'Idempotency-Key: k1' \\\n  https://x/v1/transfers \\\n  | jq .",
        "psql \\\n  postgresql://qa@localhost/payments \\\n  <<'SQL'\nselect *\n  from transfers\n where id = 'x';\nSQL",
    ],
)
def test_allowed(cmd):
    validate_command(cmd, ALLOWED)


@pytest.mark.parametrize(
    "cmd",
    [
        "",
        "rm -rf /",
        "kubectl get pods; rm -rf /",
        "kubectl get pods | sh",
        "kubectl get pods && bash -c 'x'",
        "kubectl get $(whoami)",
        'curl "`id`"',
        "kubectl get pods > /etc/passwd",
        "kubectl get pods >> out.txt",
        "kubectl get pods\nrm -rf /",
        "kubectl get pods |",
        "FOO=bar",
        "curl 'unterminated",
        # background jobs
        "kubectl port-forward svc/x 8080 &",
        "kubectl get pods & curl x",
        "nohup kubectl get pods",
        "kubectl get pods; disown",
        # heredoc shape
        "psql <<'SQL'",
        "psql <<'SQL'\nselect 1;\nEND",
        "psql <<'SQL'\nselect 1;\nSQL\nrm -rf /\nSQL",
        "psql <<A <<B\nx\nB",
        # forbidden SQL
        "psql -c 'truncate transfers'",
        "psql -c 'DROP TABLE transfers'",
        "psql <<'SQL'\ndelete from transfers;\nSQL",
        "psql <<'SQL'\nUPDATE transfers SET state = 'X';\nSQL",
        "psql -c \"delete from a where id = 1; delete from b\"",
        # line continuations
        "curl -s \\\n  https://x \\\n  | sh",
        "curl -s \\",
        "curl -s \\\\\nrm -rf /",  # escaped backslash is not a continuation
        "psql \\\n  <<'SQL'\ndelete from transfers;\nSQL",
    ],
)
def test_rejected(cmd):
    with pytest.raises(CommandNotAllowed):
        validate_command(cmd, ALLOWED)
