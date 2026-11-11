from dataclasses import replace

from conftest import BOB
from fastapi.testclient import TestClient
from test_server import server as server

from web_service.schema_history import SQLiteHistory
from web_service.server import create_test_server
from web_service.storage import digest


def test_classroom_snapshot_is_private_and_disconnect_revokes_upload(server):
    history = SQLiteHistory(server.settings.database_path, "sqlite-server")
    history.baseline(expected_sha256=history.plan()["plan_sha256"])
    history.upgrade("classroom", expected_sha256=history.plan("classroom")["plan_sha256"])
    app = create_test_server(
        replace(server.settings, classroom_enabled=True),
        server.db,
        server.identity,
        server.graph,
        server.source,
        server.sender,
        worker=False,
    )
    owner = server.client()
    other = server.client(BOB)
    with TestClient(app, base_url=server.settings.origin) as client:
        client.cookies.update(owner.cookies)
        assert client.get("/api/classroom").json() == {"connected": False, "snapshot": None}
        assert client.post("/api/classroom/connection").status_code == 403
        client.headers["x-csrf-token"] = owner.headers["x-csrf-token"]
        connected = client.post("/api/classroom/connection")
        assert connected.status_code == 201
        token = connected.json()["token"]
        assert client.post("/api/classroom/connection").status_code == 409
        payload = {
            "captured_at": server.now[0] * 1000,
            "items": [
                {"kind": "announcement", "course": "Computing", "title": "Private announcement"},
                {
                    "kind": "assignment",
                    "course": "Computing",
                    "title": "Private assignment",
                    "due": "2026-10-22",
                    "url": "https://classroom.google.com/c/example",
                },
            ],
        }
        with TestClient(app, base_url=server.settings.origin) as script:
            assert script.post("/api/classroom/import", json=payload).status_code == 401
            script.headers["authorization"] = f"Bearer {token}"
            assert script.post("/api/classroom/import", json=payload).status_code == 204
            assert (
                script.post(
                    "/api/classroom/import",
                    json={**payload, "captured_at": payload["captured_at"] - 1, "items": []},
                ).status_code
                == 204
            )
            result = client.get("/api/classroom")
            assert result.headers["cache-control"] == "no-store"
            assert [item["title"] for item in result.json()["snapshot"]["items"]] == [
                "Private announcement",
                "Private assignment",
            ]
            with server.db.connect() as db:
                row = db.execute(
                    "SELECT token_hash,payload_blob FROM web_classroom_connections"
                ).fetchone()
                assert row[0] == digest(token)
                assert "Private" not in row[1]
                assert server.db.unseal(row[1])["items"][0]["course"] == "Computing"
            with TestClient(
                app, base_url=server.settings.origin, cookies=other.cookies
            ) as outsider:
                assert outsider.get("/api/classroom").json() == {
                    "connected": False,
                    "snapshot": None,
                }
            assert client.delete("/api/classroom/connection").status_code == 204
            assert client.get("/api/classroom").json() == {"connected": False, "snapshot": None}
            assert script.post("/api/classroom/import", json=payload).status_code == 401
