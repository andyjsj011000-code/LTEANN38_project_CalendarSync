"""Explicit shared-ingestion opt-in is safe to inspect and required to provision."""

import pytest
from test_hosted import config_env as config_env

from web_service import hosted


def test_shared_schema_opt_in_and_database_flag(config_env, monkeypatch):
    import web_service.postgres_shared_schema as schema
    import web_service.server as server

    calls, databases = [], []
    monkeypatch.setattr(
        schema,
        "enable_shared_channel_schema",
        lambda connection, tenant: calls.append((connection.schema, tenant)),
    )
    monkeypatch.setattr(
        server,
        "create_test_server",
        lambda settings, database, *args, **kwargs: databases.append(database) or "app",
    )
    assert hosted.create_hosted_app(config_env, worker=False) == "app"
    assert calls == [] and databases[-1].shared_ingestion_enabled is False
    env = {**config_env, "WEB_SHARED_CHANNEL_INGESTION": "1"}
    assert hosted.configuration(env).describe()["shared_channel_ingestion"] is True
    assert hosted.create_hosted_app(env, worker=False) == "app"
    assert calls == [("calendarsync_web", config_env["WEB_ENTRA_TENANT_ID"])]
    assert databases[-1].shared_ingestion_enabled is True
    with pytest.raises(ValueError):
        hosted.configuration({**config_env, "WEB_SHARED_CHANNEL_INGESTION": "invalid"})
