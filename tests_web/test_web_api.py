from web_service.api import SESSION_COOKIE, create_app
from web_service.storage import Database


def task_payload(**values):
    return {"title": "개발 완료하기", "start_date": "2026-10-20", "end_date": "2026-10-24", **values}


def calendar(client, **query):
    return client.get("/api/calendar", params={"start": "2026-10-01", "end": "2026-10-31", **query})


def create_task(client, **values):
    response = client.post("/api/tasks", json=task_payload(**values))
    assert response.status_code == 201, response.text
    return response.json()


def test_unauthenticated_identity_headers_do_not_grant_access(env):
    c = env.client()
    for path in ("/api/me", "/api/settings", "/api/channels", "/api/today", "/api/calendar?start=2026-10-01&end=2026-10-31"):
        assert c.get(path, headers={"x-user-id": env.alice}).status_code == 401


def test_personal_data_does_not_leak_across_students(env):
    a, b = env.client(env.alice), env.client(env.bob)
    item = create_task(a)
    assert b.get(f"/api/tasks/{item['id']}").status_code == 404
    assert calendar(b).json()["items"] == []
    assert b.put(f"/api/tasks/{item['id']}", json=task_payload(version=1)).status_code == 404
    assert b.patch(f"/api/tasks/{item['id']}/state", json={"version": 1, "hidden": True}).status_code == 404
    assert b.delete(f"/api/tasks/{item['id']}?version=1").status_code == 404


def test_cannot_assign_task_to_someone_else(env):
    c = env.client(env.alice)
    assert c.post("/api/tasks", json=task_payload(user_id=env.bob)).status_code == 422


def test_task_validation_rejects_invalid_period_and_outside_dates(env):
    c = env.client(env.alice)
    assert c.post("/api/tasks", json=task_payload(end_date="2026-10-19")).status_code == 422
    assert c.post("/api/tasks", json=task_payload(display_mode="selected", selected_dates=["2026-10-25"])).status_code == 422
    assert c.post("/api/tasks", json=task_payload(title="  ")).status_code == 422


def test_selected_dates_only_affect_agenda_not_calendar(env):
    c = env.client(env.alice)
    item = create_task(c, display_mode="selected", selected_dates=["2026-10-20"])
    assert item["selected_dates"] == ["2026-10-20", "2026-10-24"]
    assert calendar(c).json()["items"][0]["end_date"] == "2026-10-24"
    assert c.get("/api/today?day=2026-10-22").json()["personal"] == []
    assert len(c.get("/api/today?day=2026-10-24").json()["personal"]) == 1


def test_final_only_and_daily_agenda_rules(env):
    c = env.client(env.alice)
    last = create_task(c, display_mode="final")
    daily = create_task(c, display_mode="daily")
    assert [e["id"] for e in c.get("/api/today?day=2026-10-22").json()["personal"]] == [daily["id"]]
    assert {e["id"] for e in c.get("/api/today?day=2026-10-24").json()["personal"]} == {last["id"], daily["id"]}


def test_unfinished_past_task_is_not_carried_to_today(env):
    c = env.client(env.alice)
    create_task(c, start_date="2026-10-21", end_date="2026-10-21")
    assert c.get("/api/today").json()["personal"] == []
    assert len(c.get("/api/today?day=2026-10-21").json()["personal"]) == 1


def test_default_today_uses_korean_date(env):
    env.now[0] += 16 * 60 * 60  # UTC Oct 22 16:00 -> Korea Oct 23 01:00
    c = env.client(env.alice)
    assert c.get("/api/today").json()["date"] == "2026-10-23"


def test_completion_keeps_original_order(env):
    c = env.client(env.alice)
    first, second = create_task(c), create_task(c, title="다른 할 일")
    before = [e["id"] for e in calendar(c).json()["items"]]
    r = c.patch(f"/api/tasks/{first['id']}/state", json={"version": 1, "completed": True})
    assert r.status_code == 200 and r.json()["completed"] is True
    assert [e["id"] for e in calendar(c).json()["items"]] == before


def test_hiding_and_restoring_personal_task(env):
    c = env.client(env.alice)
    item = create_task(c)
    assert c.patch(f"/api/tasks/{item['id']}/state", json={"version": 1, "hidden": True}).status_code == 200
    assert calendar(c).json()["items"] == []
    assert c.get("/api/hidden?start=2026-10-01&end=2026-10-31").json()["items"][0]["id"] == item["id"]
    assert c.patch(f"/api/tasks/{item['id']}/state", json={"version": 2, "hidden": False}).status_code == 200
    assert len(calendar(c).json()["items"]) == 1


def test_stale_update_and_delete_are_rejected(env):
    c = env.client(env.alice)
    item = create_task(c)
    url = f"/api/tasks/{item['id']}"
    assert c.put(url, json=task_payload(title="첫 기기의 수정", version=1)).status_code == 200
    assert c.put(url, json=task_payload(title="두 번째 기기의 오래된 수정", version=1)).status_code == 409
    assert c.delete(url + "?version=1").status_code == 409
    assert c.get(url).json()["title"] == "첫 기기의 수정"
    assert c.delete(url + "?version=2").status_code == 204
    assert c.get(url).status_code == 404


def test_another_session_sees_persisted_tasks(env):
    a = env.client(env.alice)
    item = create_task(a)
    other_device = env.client(env.alice)
    assert other_device.get(f"/api/tasks/{item['id']}").json()["title"] == item["title"]
    restarted = Database(env.settings.database_path, env.settings.data_key, env.db.clock)
    restarted.initialize()
    assert restarted.task(env.alice, item["id"])["title"] == item["title"]


def test_personal_text_is_encrypted_at_rest_and_session_is_hashed(env):
    c = env.client(env.alice)
    create_task(c, title="unique-private-task-5fbae720", notes="unique-private-notes-56a2")
    content = env.settings.database_path.read_bytes()
    assert b"unique-private-task-5fbae720" not in content
    assert b"unique-private-notes-56a2" not in content
    assert c.cookies.get(SESSION_COOKIE).encode() not in content


def test_shared_access_is_current_and_user_scoped(shared):
    a, b = shared.client(shared.alice), shared.client(shared.bob)
    assert [e["id"] for e in calendar(a).json()["items"]] == ["event-a"]
    assert [e["id"] for e in calendar(b).json()["items"]] == ["event-b"]
    assert b.patch("/api/events/event-a/state", json={"hidden": True}).status_code == 404
    shared.now[0] += 3601
    assert calendar(a).json()["items"] == []
    assert a.get("/api/channels").json() == []


def test_removed_access_takes_effect_immediately(shared):
    c = shared.client(shared.alice)
    with shared.db.connect() as conn:
        conn.execute("DELETE FROM web_channel_access WHERE user_id=?", (shared.alice,))
    assert calendar(c).json()["items"] == []
    assert c.patch("/api/events/event-a/state", json={"completed": True}).status_code == 404


def test_channel_defaults_on_and_preference_is_private(shared):
    c = shared.client(shared.alice)
    assert c.get("/api/channels").json()[0]["enabled"] is True
    assert c.put("/api/channels/channel-a/preference", json={"enabled": False}).status_code == 200
    assert calendar(c).json()["items"] == []
    assert c.put("/api/channels/channel-b/preference", json={"enabled": False}).status_code == 404
    assert len(calendar(shared.client(shared.bob)).json()["items"]) == 1


def test_type_filter_and_hidden_restoration(shared):
    c = shared.client(shared.alice)
    assert c.put("/api/settings", json={"disabled_kinds": ["assignment"]}).status_code == 200
    assert calendar(c).json()["items"] == []
    c.patch("/api/events/event-a/state", json={"hidden": True})
    hidden = c.get("/api/hidden?start=2026-10-01&end=2026-10-31").json()["items"]
    assert [e["id"] for e in hidden] == ["event-a"]
    c.patch("/api/events/event-a/state", json={"hidden": False})
    assert calendar(c).json()["items"] == []  # Still filtered by kind
    c.put("/api/settings", json={"disabled_kinds": []})
    assert len(calendar(c).json()["items"]) == 1


def test_cancelled_event_remains_visible(shared):
    c = shared.client(shared.alice)
    with shared.db.connect() as conn:
        conn.execute("UPDATE web_events SET cancelled=1 WHERE id='event-a'")
    assert calendar(c).json()["items"][0]["cancelled"] is True


def test_calendar_export_off_does_not_block_web(shared):
    c = shared.client(shared.alice)
    data = c.get("/api/settings").json()
    assert data["preferences"]["calendar_sync_enabled"] is False
    assert data["capabilities"]["calendar_export"] is False
    assert len(calendar(c).json()["items"]) == 1
    assert c.put("/api/settings", json={"calendar_sync_enabled": True}).status_code == 409


def test_query_range_and_settings_are_validated(env):
    c = env.client(env.alice)
    assert calendar(c, end="2026-09-30").status_code == 422
    assert calendar(c, end="2030-01-01").status_code == 422
    assert c.put("/api/settings", json={"morning_time": "25:00"}).status_code == 422
    assert c.put("/api/settings", json={"morning_time": "06:30", "morning_enabled": False}).status_code == 200
    assert c.get("/api/settings").json()["preferences"]["morning_time"] == "06:30"


def test_no_legacy_oracle_or_scheduler_import(env):
    import sys
    assert "app.core.dependencies" not in sys.modules
    assert "app.tasks.scheduler" not in sys.modules
