from conftest import ALICE, BOB
from test_server import server as server

from web_service.calendar_rules import merge_teams_reposts


def test_teams_calendar_merges_reposts_and_only_links_accessible_posts(server, monkeypatch):
    channels = [
        {"id": "grade", "team_id": "grade-team", "name": "학년 공지"},
        {"id": "school", "team_id": "school-team", "name": "학교 공지"},
    ]
    monkeypatch.setattr(
        server.graph, "channels", lambda _, oid, __: channels if oid == ALICE else channels[:1]
    )
    initial = {
        "id": "first",
        "channel_id": "grade",
        "title": "1,3학년 실험실 안전 점검(안) 설명회",
        "notes": "별관 2층 회의실",
        "start_date": "2026-11-10",
        "end_date": "2026-11-10",
        "time": "16:10",
        "kind": "event",
        "cancelled": False,
        "source_url": "https://teams.microsoft.com/l/message/grade/1",
    }
    repost = {
        **initial,
        "id": "repost",
        "channel_id": "school",
        "title": "1·3학년 실험실 안전점검(안) 설명회",
        "notes": "새 점검 기준과 실습실 이용 절차를 안내합니다.",
        "source_url": "https://teams.microsoft.com/l/message/school/2",
    }
    another = {
        **repost,
        "id": "another",
        "title": "2학년 실험실 안전 점검(안) 설명회",
        "source_url": "https://teams.microsoft.com/l/message/school/3",
    }
    later = {
        **repost, "id": "later", "time": "17:00",
        "source_url": "https://teams.microsoft.com/l/message/school/4",
    }
    items = [initial, repost, another, later]

    def collect(visible, _):
        allowed = {channel["id"] for channel in visible}
        return [item for item in items if item["channel_id"] in allowed]

    monkeypatch.setattr(server.source, "events", collect)
    owner, grade_only = server.client(), server.client(BOB)
    path = "/api/calendar?start=2026-11-10&end=2026-11-10"
    calendar = owner.get(path).json()["items"]
    assert {item["id"] for item in calendar} == {"oracle:first", "oracle:another", "oracle:later"}
    merged = next(item for item in calendar if item["id"] == "oracle:first")
    assert [source["url"] for source in merged["related_sources"]] == [
        initial["source_url"], repost["source_url"],
    ]
    assert owner.get("/api/today?day=2026-11-10").json()["teams"] == calendar
    restricted = grade_only.get(path).json()["items"]
    assert [item["id"] for item in restricted] == ["oracle:first"]
    assert "related_sources" not in restricted[0]


def test_teams_calendar_requires_context_for_generic_or_similar_titles():
    base = {
        "source": "teams", "channel": "학교 공지", "channel_id": "school",
        "start_date": "2026-11-10", "end_date": "2026-11-10", "time": None,
        "kind": "event", "cancelled": False, "completed": False,
    }

    def row(post, title, notes):
        return {
            **base, "id": post, "title": title, "notes": notes,
            "source_url": f"https://teams.microsoft.com/l/message/school/{post}",
        }

    notes = "별관 2층 회의실에서 새 실험실 점검 기준을 안내합니다."
    first = row("one", "실험실 안전 점검 기준 변경 설명회", notes)
    second = row("two", "실험실 안전 점검 기준 변경 설명회 재안내", notes)
    generic = row("three", "참가 신청", "화학 실험 발표 참여 신청 안내입니다.")
    other = row("four", "참가 신청", "천년제 공연팀 무대 지원 신청입니다.")
    result = merge_teams_reposts([first, second, generic, other])
    assert [item["id"] for item in result] == ["one", "three", "four"]
    assert [source["id"] for source in result[0]["related_sources"]] == ["one", "two"]
