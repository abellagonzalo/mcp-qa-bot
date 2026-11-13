import re

import pytest

from qa_bot.policy import Policy, Refused, read_pane_env


def no_kube(pane_env):
    return {}


def check(command, pane_env=None, kube=None):
    Policy(kube_lookup=lambda env: kube or {}).check(command, pane_env or {})


# ------------------------------------------------------------------ hard limits


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
        # any executable may run: there is no allowlist
        "proxdb -r payments -c 'select 1'",
        "tsh status",
        "source ~/.config/qa/staging.env",
        "echo eyJhbGciOi.eyJleHAiOjE3MDB9.sig | cut -d. -f2 | base64 -d",
        "gh pr view 42 --json headRefOid | jq -r .headRefOid",
        "kubectl get pods | sh",
        "curl -s \\\n  https://x \\\n  | sh",
        # loops
        "for i in 1 2 3; do curl -s https://x/v1/batches/276 | jq .status; sleep 20; done",
        "for i in {1..5}\ndo\n  curl -s https://x/$i | jq .\ndone",
        "for i in {1..10}; do\n  curl -s -o /dev/null -w '%{http_code}\\n' \\\n    -X POST https://x/v1/transfers \\\n    -d '{\"n\": 1}'\n  sleep 1\ndone",
        "while true; do\n  curl -s https://x | jq -e '.status == \"DONE\"' && break\n  sleep 20\ndone",
        "until curl -sf https://x/health; do\n  sleep 5\ndone",
        "for i in 1 2; do\n  for j in a b; do\n    echo $i$j\n  done\ndone",
        "for i in 1 2; do\n  echo $i\ndone | jq -s .",
    ],
)
def test_allowed(cmd):
    check(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "",
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
        "curl -s \\",
        "curl -s \\\\\nrm -rf /",  # escaped backslash is not a continuation
        "psql \\\n  <<'SQL'\ndelete from transfers;\nSQL",
        # loops: every other limit still applies inside them
        "for i in 1 2; do\n  curl -s https://x/$(whoami)\ndone",
        "for i in 1 2; do\n  curl -s https://x > out.txt\ndone",
        "for i in 1 2; do\n  curl -s https://x &\ndone",
        "for i in 1 2; do\n  psql -c 'truncate transfers'\ndone",
        "for i in 1 2; do\n  psql -c 'delete from transfers'\ndone",
        "for i in 1 2; do\n  psql <<'SQL'\nselect 1;\nSQL\ndone",
        "for i in 1 2; do\n  echo 'a\nb'\ndone",  # newline inside quotes
        "for i in 1 2; do\n  echo $i\n",  # no done
        "echo a\nfor i in 1; do echo $i; done",  # loop not first
        "for i in 1 2; do\n  echo $i |\n",
    ],
)
def test_rejected(cmd):
    with pytest.raises(Refused):
        check(cmd)


# ------------------------------------------------------------- production guard


@pytest.mark.parametrize(
    "command,env,kube",
    [
        ("kubectl --context prod-eu get pods", {}, {}),
        ("curl https://payments.production.internal/v1", {}, {}),
        ("psql -h db-prd.internal", {}, {}),
        ("kubectl get pods", {}, {"context": "eu-prod1"}),
        ("kubectl get pods", {}, {"server": "https://k8s.prod.internal"}),
        ("psql -c 'select 1'", {"PGHOST": "payments.prod.rds"}, {}),
        ("psql", {"DATABASE_URL": "postgres://u:secret@db.prod/x"}, {}),
    ],
)
def test_production_refused(command, env, kube):
    with pytest.raises(Refused) as exc:
        check(command, env, kube)
    assert "secret" not in str(exc.value)
    assert str(exc.value) == f"Refused: {exc.value.reason}"


@pytest.mark.parametrize(
    "command,env,kube",
    [
        ("curl https://x.staging.internal/v1/products", {}, {}),
        ("kubectl get pods", {"PGHOST": "db.staging"}, {"context": "staging-eu", "namespace": "payments"}),
        ("curl https://x.dev/health/live", {}, {}),
        ("kubectl get pods", {}, {}),
    ],
)
def test_non_production_allowed(command, env, kube):
    check(command, env, kube)


def test_kube_lookup_gets_the_pane_env():
    seen = []
    Policy(kube_lookup=lambda env: seen.append(env) or {}).check("kubectl get pods", {"KUBECONFIG": "/k"})
    assert seen == [{"KUBECONFIG": "/k"}]


@pytest.mark.parametrize("command", ["kubectl get $(whoami)", "kubectl --context prod get pods"])
def test_text_refusals_skip_the_kube_lookup(command):
    def boom(env):
        raise AssertionError("kube lookup should not run")

    with pytest.raises(Refused):
        Policy(kube_lookup=boom).check(command, {})


def test_prod_pattern_is_configurable(monkeypatch):
    monkeypatch.setenv("QA_BOT_PROD_PATTERN", r"\blive\b")
    with pytest.raises(Refused):
        Policy(kube_lookup=no_kube).check("curl https://api.live.example", {})
    Policy(kube_lookup=no_kube).check("curl https://api.prod.example", {})
    Policy(kube_lookup=no_kube, prod_pattern=re.compile("x9")).check("curl https://api.prod.example", {})


def test_read_pane_env(tmp_path):
    f = tmp_path / "env"
    f.write_text("PGHOST=db.staging\nPGSERVICE=\nKUBECONFIG=/a=b\njunk\n")
    assert read_pane_env(f) == {"PGHOST": "db.staging", "KUBECONFIG": "/a=b"}
    assert read_pane_env(tmp_path / "missing") == {}
