"""Beta configuration/preflight boundary checks; no live providers or database."""

import builtins
import json
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from conftest import TENANT, CLIENT
from web_service import hosted


@pytest.fixture
def config_env():
    return {
        "WEB_STORAGE_BACKEND": "postgresql",
        "WEB_DATA_KEY": Fernet.generate_key().decode(),
        "WEB_ORIGIN": "https://beta.example.test",
        "WEB_ENTRA_TENANT_ID": TENANT,
        "WEB_ENTRA_CLIENT_ID": CLIENT,
        "WEB_ENTRA_CLIENT_SECRET": "private-entra",
        "DATABASE_URL": "postgresql://private_user:private_password@db.example.test/private_db",
        "WEB_OPENAI_API_KEY": "private-ai",
    }


def test_config_preflight_does_not_import_driver_or_reveal_secrets(config_env, monkeypatch):
    original = builtins.__import__

    def no_driver(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            pytest.fail("Configuration checks must not import or connect a database driver")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_driver)
    result = hosted.configuration(config_env)
    assert result.describe()["callback_url"] == "https://beta.example.test/auth/callback"
    assert result.describe()["automatic_schema_changes"] is False
    text = repr(result) + json.dumps(result.describe())
    for key in ("WEB_DATA_KEY", "WEB_ENTRA_CLIENT_SECRET", "WEB_OPENAI_API_KEY", "DATABASE_URL"):
        assert config_env[key] not in text
    for value in ("private_user", "private_password", "private_db", "db.example.test"):
        assert value not in text


@pytest.mark.parametrize(
    "change",
    [
        {"WEB_STORAGE_BACKEND": "sqlite"},
        {"WEB_INGESTION_MODE": "oracle"},
        {"WEB_ORIGIN": "http://localhost:8080"},
        {"WEB_ENTRA_CLIENT_SECRET": ""},
        {"WEB_OPENAI_API_KEY": ""},
        {"WEB_DATABASE_SCHEMA": "public"},
        {"WEB_DATABASE_SCHEMA": "information_schema"},
        {"WEB_DATABASE_SCHEMA": "other;DROP"},
        {"WEB_AI_IMAGES_ENABLED": "maybe"},
        {"WEB_DATABASE_SSLMODE": "disable"},
        {"WEB_BOOTSTRAP_NEW_SCHEMA": "maybe"},
    ],
)
def test_unready_or_unsafe_hosted_configuration_is_rejected(config_env, change):
    with pytest.raises(ValueError):
        hosted.configuration({**config_env, **change})


def test_internal_tls_requirement_is_explicit_and_not_an_automatic_fallback(config_env):
    assert hosted.configuration(config_env).connection.sslmode == "verify-full"
    assert (
        hosted.configuration({**config_env, "WEB_DATABASE_SSLMODE": "require"}).connection.sslmode
        == "require"
    )


def test_explicit_hosted_factory_passes_separate_pg_adapter_without_notification_mode(
    config_env, monkeypatch
):
    import web_service.server as server

    calls = []

    def create(settings, database, identity, graph, source, sender, **kwargs):
        calls.append((settings, database, identity, graph, source, sender, kwargs))
        return "app"

    monkeypatch.setattr(server, "create_test_server", create)
    graph, source, identity, sender = object(), object(), object(), object()
    assert (
        hosted.create_hosted_app(
            config_env, identity=identity, graph=graph, source=source, sender=sender, worker=False
        )
        == "app"
    )
    settings, db, provider, actual_graph, actual_source, actual_sender, kwargs = calls[0]
    assert db.tenant_id == TENANT and db.storage_backend == "postgresql-experimental"
    assert db.connect.describe()["schema"] == "calendarsync_web"
    assert (provider, actual_graph, actual_source, actual_sender) == (
        identity,
        graph,
        source,
        sender,
    )
    assert kwargs == {"worker": False}


def test_render_origin_fallback_is_explicit_https_and_keeps_operator_override(config_env):
    env = {**config_env, "WEB_ORIGIN": "", "RENDER_EXTERNAL_URL": "https://assigned.onrender.com"}
    assert hosted.configuration(env).settings.origin == "https://assigned.onrender.com"
    assert (
        hosted.configuration({**env, "WEB_ORIGIN": "https://custom.example.test"}).settings.origin
        == "https://custom.example.test"
    )
    with pytest.raises(ValueError):
        hosted.configuration({**env, "RENDER_EXTERNAL_URL": "http://untrusted.example.test"})


@pytest.mark.parametrize("exists", [False, True])
def test_schema_creation_requires_opt_in_and_never_applies_to_existing_namespace(
    config_env, monkeypatch, exists
):
    import web_service.postgres_bootstrap as bootstrap
    import web_service.server as server

    applied = []
    monkeypatch.setattr(bootstrap, "schema_exists", lambda factory: exists)
    monkeypatch.setattr(
        bootstrap, "apply_bootstrap", lambda factory: applied.append(factory.schema)
    )
    monkeypatch.setattr(server, "create_test_server", lambda *args, **kwargs: "app")
    hosted.create_hosted_app(config_env, worker=False)
    assert applied == []
    configured = {**config_env, "WEB_BOOTSTRAP_NEW_SCHEMA": "1"}
    assert hosted.configuration(configured).describe()["automatic_schema_changes"] is True
    assert hosted.create_hosted_app(configured, worker=False) == "app"
    assert applied == ([] if exists else ["calendarsync_web"])


def test_failed_schema_creation_does_not_launch_or_fall_back_to_sqlite(config_env, monkeypatch):
    import web_service.postgres_bootstrap as bootstrap
    import web_service.server as server

    def fail(factory):
        raise bootstrap.BootstrapRejected("Rejected test bootstrap")

    monkeypatch.setattr(bootstrap, "schema_exists", lambda factory: False)
    monkeypatch.setattr(bootstrap, "apply_bootstrap", fail)
    monkeypatch.setattr(
        server,
        "create_test_server",
        lambda *args, **kwargs: pytest.fail("A failed bootstrap must not start the app"),
    )
    with pytest.raises(bootstrap.BootstrapRejected):
        hosted.create_hosted_app({**config_env, "WEB_BOOTSTRAP_NEW_SCHEMA": "1"}, worker=False)
