"""Entry-point compatibility and isolation without opening provider connections."""

import pytest
from conftest import CLIENT, TENANT
from cryptography.fernet import Fernet

from web_service.composition import compose_server
from web_service.config import DirectSourceConfig, OracleConfig, PushConfig, Settings
from web_service.direct_source import DirectSource, TeamsGraph, direct_source_from_env
from web_service.oracle_source import NoSource, OracleSource, source_from_env
from web_service.push import sender_from_env
from web_service.test_storage import TestDatabase


@pytest.fixture
def settings_env(tmp_path):
    return {
        "WEB_DATA_KEY": Fernet.generate_key().decode(),
        "WEB_DATABASE_PATH": str(tmp_path / "server.sqlite3"),
        "WEB_ORIGIN": "https://calendar.example.test",
        "WEB_ENTRA_TENANT_ID": TENANT,
        "WEB_ENTRA_CLIENT_ID": CLIENT,
        "WEB_ENTRA_CLIENT_SECRET": "test-only-secret",
    }


def test_settings_mapping_does_not_read_or_mutate_process_environment(settings_env, monkeypatch):
    monkeypatch.setenv("WEB_ORIGIN", "not-an-origin")
    monkeypatch.setenv("WEB_SESSION_SECONDS", "not-a-number")
    before = dict(settings_env)
    settings = Settings.from_env(settings_env)
    assert settings.origin == settings_env["WEB_ORIGIN"]
    assert settings_env == before


def test_empty_mapping_does_not_fall_back_to_global_key(settings_env, monkeypatch):
    monkeypatch.setenv("WEB_DATA_KEY", settings_env["WEB_DATA_KEY"])
    with pytest.raises(ValueError, match="WEB_DATA_KEY is required"):
        Settings.from_env({})


def test_entry_points_keep_distinct_session_defaults(settings_env):
    foundation = Settings.from_env(settings_env)
    server = Settings.from_server_env(settings_env)
    assert (foundation.session_seconds, foundation.idle_seconds) == (3600, 900)
    assert (server.session_seconds, server.idle_seconds) == (28800, 1800)
    overrides = {**settings_env, "WEB_SESSION_SECONDS": "7200", "WEB_IDLE_SECONDS": "600"}
    for settings in (Settings.from_env(overrides), Settings.from_server_env(overrides)):
        assert (settings.session_seconds, settings.idle_seconds) == (7200, 600)


def test_no_argument_settings_reader_still_uses_process_environment(settings_env, monkeypatch):
    for key in (
        "WEB_SESSION_SECONDS",
        "WEB_IDLE_SECONDS",
        "WEB_ACCESS_SECONDS",
        "WEB_ALLOW_LOCAL_LOGIN",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in settings_env.items():
        monkeypatch.setenv(key, value)
    assert Settings.from_env() == Settings.from_env(settings_env)


@pytest.mark.parametrize("flag, expected", [("1", True), ("0", False), ("other", False)])
def test_direct_factory_keeps_model_key_and_legacy_image_flag(flag, expected, monkeypatch):
    monkeypatch.setenv("WEB_LLM_MODEL", "wrong-global-model")
    graph = TeamsGraph()
    env = {
        "WEB_OPENAI_API_KEY": "test-ai-key",
        "WEB_LLM_MODEL": "test-model",
        "WEB_AI_IMAGES_ENABLED": flag,
    }
    source = direct_source_from_env(graph, env)
    assert source.graph is graph
    assert source.extractor.key == "test-ai-key"
    assert source.extractor.model == "test-model"
    assert source.images is expected
    assert "test-ai-key" not in repr(DirectSourceConfig.from_env(env))


def test_oracle_factory_preserves_none_partial_and_complete_configuration(settings_env):
    assert isinstance(source_from_env({}), NoSource)
    with pytest.raises(ValueError, match="Configure all WEB_ORACLE_"):
        source_from_env({"WEB_ORACLE_USER": "reader"})
    env = {
        "WEB_ORACLE_USER": "reader",
        "WEB_ORACLE_PASSWORD": "test-oracle-password",
        "WEB_ORACLE_DSN": "test-dsn",
        "WEB_ORACLE_DATA_KEY": settings_env["WEB_DATA_KEY"],
        "WEB_ORACLE_WALLET_DIR": "test-wallet",
        "WEB_ORACLE_WALLET_PASSWORD": "test-wallet-password",
    }
    source = source_from_env(env)
    assert isinstance(source, OracleSource)
    assert source.config == OracleConfig.from_env(env)
    assert source.config.wallet_dir == "test-wallet"
    assert "test-oracle-password" not in repr(source.config)
    assert "test-wallet-password" not in repr(source.config)


def test_push_factory_keeps_disabled_and_partial_credential_behavior():
    assert sender_from_env({}).configured is False
    with pytest.raises(ValueError, match="Configure both VAPID values"):
        sender_from_env({"WEB_VAPID_SUBJECT": "mailto:test@example.invalid"})
    assert "test-private-key" not in repr(PushConfig(private="test-private-key"))


def test_injected_components_skip_unrelated_invalid_environment(settings_env):
    settings = Settings.from_env(settings_env)
    db = TestDatabase(settings.database_path, settings.data_key)
    identity, graph, source, sender = object(), object(), object(), object()
    result = compose_server(
        settings,
        db,
        identity,
        graph,
        source,
        sender,
        environ={
            "RENDER": "true",
            "DATABASE_URL": "configured-but-explicitly-injected",
            "WEB_DATA_KEY": "invalid",
            "WEB_SESSION_SECONDS": "invalid",
            "WEB_INGESTION_MODE": "invalid",
            "WEB_VAPID_SUBJECT": "invalid",
        },
    )
    assert result.settings is settings and result.database is db
    assert (result.identity, result.graph, result.source, result.sender) == (
        identity,
        graph,
        source,
        sender,
    )
    assert not settings.database_path.exists()


@pytest.mark.parametrize("mode, source_type", [("direct", DirectSource), ("oracle", NoSource)])
def test_composition_selects_mode_without_initializing_storage(settings_env, mode, source_type):
    result = compose_server(environ={**settings_env, "WEB_INGESTION_MODE": mode})
    assert isinstance(result.source, source_type)
    assert isinstance(result.graph, TeamsGraph)
    assert result.sender.configured is False
    assert result.settings.session_seconds == 28800
    assert not result.settings.database_path.exists()


def test_invalid_mode_is_rejected_before_any_storage_initialization(settings_env):
    with pytest.raises(ValueError, match="Invalid ingestion mode"):
        compose_server(environ={**settings_env, "WEB_INGESTION_MODE": "invalid"})
    assert not Settings.from_env(settings_env).database_path.exists()


def test_hosted_guard_precedes_missing_or_invalid_settings():
    with pytest.raises(RuntimeError, match="PostgreSQL storage is not implemented"):
        compose_server(environ={"RENDER": "true", "WEB_DATA_KEY": "invalid"})
