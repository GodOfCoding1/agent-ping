"""Protect cloud credentials from accidental production database probes."""

import pytest
from smoke import guarded_url


@pytest.fixture
def context():
    return {
        "project_id": "empty-surf-03967309",
        "production_branch_id": "br-production",
        "development": {
            "branch_id": "br-development",
            "pooled_host": "dev-pooler.example.invalid",
            "direct_host": "dev.example.invalid",
        },
    }


def environment(host="dev-pooler.example.invalid", sslmode="require"):
    return {
        "APP_ENV": "development",
        "DATABASE_URL": f"postgresql://user:private@{host}/db?sslmode={sslmode}",
    }


def test_rejects_production_host_even_when_environment_says_development(context):
    with pytest.raises(ValueError, match="Endpoint"):
        guarded_url("DATABASE_URL", environment("production.example.invalid"), context)


def test_rejects_production_branch_even_with_matching_hostname(context):
    context["development"]["branch_id"] = "br-production"
    with pytest.raises(ValueError, match="Production"):
        guarded_url("DATABASE_URL", environment(), context)


def test_requires_transport_security(context):
    with pytest.raises(ValueError, match="TLS"):
        guarded_url("DATABASE_URL", environment(sslmode="disable"), context)


def test_rejects_production_environment(context):
    values = environment()
    values["APP_ENV"] = "production"
    with pytest.raises(ValueError, match="APP_ENV"):
        guarded_url("DATABASE_URL", values, context)


def test_valid_context_selects_psycopg_without_exposing_password(context):
    url = guarded_url("DATABASE_URL", environment(), context)
    assert url.drivername == "postgresql+psycopg"
    assert "private" not in str(url)


def test_test_credentials_cannot_use_development_endpoint(context):
    context["test"] = {
        "branch_id": "br-test",
        "pooled_host": "test-pooler.example.invalid",
    }
    values = environment()
    values["TEST_DATABASE_URL"] = values["DATABASE_URL"]
    with pytest.raises(ValueError, match="Endpoint"):
        guarded_url("TEST_DATABASE_URL", values, context)
