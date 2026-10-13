"""Optional live OpenAI evaluation. Does not read or write Microsoft calendars."""
import asyncio
import os

from calender_service import make_event_body
from schedule_dedup import find_duplicate


def item(title, summary, kind="unknown", **extra):
    return {
        "title": title, "summary": summary,
        "start_time": "2026-10-13T00:00:00", "end_time": "2026-10-13T23:59:00",
        "event_kind": kind, "time_precision": "date", **extra,
    }


CASES = [
    ("표의 짧은 이름과 상세 휴업 공지",
     item("재량휴업", "전교생 휴업", "holiday"),
     item("학교장 재량 휴업일 안내", "10월 13일은 전교생 등교하지 않음", "holiday"), True),
    ("과목명의 다른 표기",
     item("물리학Ⅰ 중간평가", "1학년 2학기 중간고사", "exam", target_grades=[1]),
     item("물리1 중간고사 실시", "1학년 2학기 물리학Ⅰ 중간시험", "exam", target_grades=[1]), True),
    ("서술형 제출 안내와 일정 제목",
     item("과제연구 소감문 제출", "콜로퀴엄 참관 소감문 제출 마감", "submission_deadline"),
     item("콜로퀴엄 후기 제출 기한", "과제연구 콜로퀴엄 참관 후 소감문을 제출해야 함", "submission_deadline"), True),
    ("같은 날의 다른 과목",
     item("물리학 평가", "물리학Ⅰ 시험", "exam"),
     item("화학 평가", "화학Ⅰ 시험", "exam"), False),
    ("같은 행사라도 신청과 개최는 별개",
     item("과학 특강 신청", "과학 특강 신청 마감", "application_deadline"),
     item("과학 특강 개최", "초청 강사의 과학 특강 진행", "event"), False),
    ("같은 제목이지만 다른 반",
     item("보고서 제출", "물리 보고서", "submission_deadline", audience="1학년 1반"),
     item("보고서 제출", "물리 보고서", "submission_deadline", audience="1학년 2반"), False),
    ("같은 과목이라도 다른 회차",
     item("물리 보고서", "실험 1 보고서 제출", "submission_deadline"),
     item("물리 보고서", "실험 2 보고서 제출", "submission_deadline"), False),
    ("날짜만 있는 안내와 시각이 있는 동일 행사",
     item("천문학 초청 특강", "전교생 대상 강당 특강", "event"),
     item("천문학 초청 강연", "전교생 대상 강당 특강", "event",
          start_time="2026-10-13T14:00:00", end_time="2026-10-13T15:00:00", time_precision="time"), True),
]


async def main():
    failures = []
    for name, first, second, expected in CASES:
        actual = await find_duplicate(make_event_body(second), [make_event_body(first)]) is not None
        passed = actual == expected
        print(f"{'PASS' if passed else 'FAIL'}: {name} (expected={expected}, actual={actual})")
        if not passed:
            failures.append(name)
    print(f"{len(CASES) - len(failures)}/{len(CASES)} cases passed")
    return bool(failures)


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY first. This evaluation makes billable OpenAI API calls.")
    raise SystemExit(asyncio.run(main()))
