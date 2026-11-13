import pytest

from qa_bot.safety import ProductionRefused, check_not_production


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
    with pytest.raises(ProductionRefused) as exc:
        check_not_production(command, env, kube)
    assert "secret" not in str(exc.value)


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
    check_not_production(command, env, kube)
