from dataclasses import replace
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from web_service.api import FLOW_COOKIE, SESSION_COOKIE, create_app
from web_service.config import Settings
from web_service.identity import MicrosoftIdentity
from conftest import TENANT, CLIENT


def finish_login(c, **params):
    return c.get("/auth/callback", params={"state": "test-state", "code": "valid-test-code", **params}, follow_redirects=False)


def test_login_server_flow_and_secure_cookie(env):
    c = env.client()
    response = c.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "server-only-verifier" not in cookie and "server-only-nonce" not in cookie
    assert finish_login(c).status_code == 303
    assert c.get("/api/me").json()["id"] == env.alice
    assert finish_login(c).status_code == 400  # Flow is single-use


def test_wrong_state_and_direct_identity_are_rejected(env):
    c = env.client()
    assert finish_login(c, user_id=env.alice).status_code == 400
    c.get("/auth/login", follow_redirects=False)
    assert finish_login(c, state="forged").status_code == 400
    assert c.get("/api/me").status_code == 401


@pytest.mark.parametrize("override", [
    {"tid": "55555555-5555-4555-8555-555555555555"},
    {"iss": "https://attacker.example/issuer"},
    {"aud": "55555555-5555-4555-8555-555555555555"},
    {"oid": "not-a-uuid"},
    {"sub": ""},
    {"exp": 1},
])
def test_rejects_wrong_tenant_issuer_audience_or_identity(env, override):
    c = env.client()
    env.identity.claim_overrides = override
    c.get("/auth/login", follow_redirects=False)
    assert finish_login(c).status_code == 400
    assert c.get("/api/me").status_code == 401


def test_expired_flow_rejected(env):
    c = env.client()
    c.get("/auth/login", follow_redirects=False)
    env.now[0] += 601
    assert finish_login(c).status_code == 400


def test_session_expiry_logout_and_forgery(env):
    c = env.client(env.alice)
    raw = c.cookies.get(SESSION_COOKIE)
    assert c.post("/auth/logout").status_code == 204
    assert env.db.session(raw) is None
    c.cookies.set(SESSION_COOKIE, raw)
    assert c.get("/api/me").status_code == 401
    c.cookies.set(SESSION_COOKIE, "forged")
    assert c.get("/api/me").status_code == 401
    d = env.client(env.alice)
    env.now[0] += env.settings.session_seconds + 1
    assert d.get("/api/me").status_code == 401


def test_disabled_user_loses_session(env):
    c = env.client(env.alice)
    with env.db.connect() as conn:
        conn.execute("UPDATE web_users SET active=0 WHERE id=?", (env.alice,))
    assert c.get("/api/me").status_code == 401


def test_csrf_and_cross_origin_mutations_rejected(env):
    c = env.client(env.alice)
    payload = {"title": "Task", "start_date": "2026-10-22"}
    assert c.post("/api/tasks", json=payload, headers={"x-csrf-token": "invalid"}).status_code == 403
    assert c.post("/api/tasks", json=payload, headers={"origin": "https://attacker.example"}).status_code == 403
    assert c.post("/api/tasks", json=payload, headers={"origin": env.settings.origin}).status_code == 201


def test_relogin_rotates_and_revokes_existing_session(env):
    c = env.client(env.alice)
    old = c.cookies.get(SESSION_COOKIE)
    # Use the response cookie to avoid an artificial duplicate domainless cookie.
    c.cookies.clear()
    c.headers["cookie"] = f"{SESSION_COOKIE}={old}"
    response = c.get("/auth/login", follow_redirects=False)
    flow_cookie = response.cookies.get(FLOW_COOKIE)
    c.headers["cookie"] += f"; {FLOW_COOKIE}={flow_cookie}"
    assert finish_login(c).status_code == 303
    assert env.db.session(old) is None


def test_unconfigured_microsoft_login_fails_closed_without_identity(env):
    config = replace(env.settings, tenant_id="", client_id="", client_secret="")
    app = create_app(config, env.db)
    with TestClient(app, base_url=config.origin) as c:
        assert c.get("/auth/login").status_code == 503
        assert c.get("/api/me").status_code == 401


def test_no_sensitive_response_cache(env):
    c = env.client(env.alice)
    r = c.get("/api/me", headers={"origin": "https://attacker.example"})
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in r.headers


def test_configuration_rejects_insecure_origin_and_partial_credentials(env):
    with pytest.raises(ValueError):
        replace(env.settings, origin="http://calendar.example.test")
    with pytest.raises(ValueError):
        replace(env.settings, origin="https://calendar.example.test/path")
    with pytest.raises(ValueError):
        replace(env.settings, client_secret="")


def test_msal_adapter_delegates_the_full_flow(monkeypatch, env):
    calls = {}
    flow = {"auth_uri": "https://login.microsoftonline.com/test", "state": "s", "nonce": "n", "code_verifier": "p"}
    class Client:
        def __init__(self, *args, **kwargs):
            calls["construction"] = (args, kwargs)
        def initiate_auth_code_flow(self, **kwargs):
            calls["begin"] = kwargs
            return flow
        def acquire_token_by_auth_code_flow(self, stored, query):
            calls["finish"] = (stored, query)
            return {"id_token_claims": {"tid": TENANT}}
    monkeypatch.setitem(sys.modules, "msal", SimpleNamespace(ConfidentialClientApplication=Client))
    provider = MicrosoftIdentity(env.settings)
    assert provider.begin() == flow
    assert provider.finish(flow, {"code": "code", "state": "s"}) == {"tid": TENANT}
    assert calls["begin"]["redirect_uri"] == env.settings.callback_url
    assert calls["finish"][0] is flow
    assert calls["construction"][1]["authority"] == f"https://login.microsoftonline.com/{TENANT}"
