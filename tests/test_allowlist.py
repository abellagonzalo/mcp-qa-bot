import pytest

from qa_server import CommandNotAllowed, validate_command

ALLOWED = {"kubectl", "curl", "psql", "grep"}


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
        "kubectl get pods\nrm -rf /",
        "kubectl get pods |",
        "FOO=bar",
        "curl 'unterminated",
    ],
)
def test_rejected(cmd):
    with pytest.raises(CommandNotAllowed):
        validate_command(cmd, ALLOWED)
