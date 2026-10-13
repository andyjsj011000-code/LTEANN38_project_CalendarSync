import asyncio
import unittest
from unittest.mock import patch

import httpx

import calender_service as calendar
import schedule_dedup as dedup


def notice(title="[물리학] 보고서 제출", start="2026-10-13T00:00:00", end="2026-10-13T23:59:00", web_url="https://teams.example/channel-1"):
    return {"title": title, "start_time": start, "end_time": end, "web_url": web_url}


def response(status, data=None):
    return httpx.Response(status, json=data or {}, request=httpx.Request("GET", "https://graph.example"))


class CalendarDedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.posts = []
        self.post_statuses = []
        self.get_pages = []

        async def request(client, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            # Force channel tasks to overlap; Graph does not reflect new POSTs.
            await asyncio.sleep(0)
            if method == "GET":
                return self.get_pages.pop(0) if self.get_pages else response(200, {"value": []})
            self.posts.append((url, kwargs["json"]))
            status = self.post_statuses.pop(0) if self.post_statuses else 201
            return response(status)

        self.patcher = patch.object(calendar, "safe_http_request", side_effect=request)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.judge_patcher = patch.object(
            dedup, "classify_candidates",
            return_value=dedup.MatchDecision(
                relation="distinct", matched_index=None, confidence=1.0, reason="mock distinct"
            ),
        )
        self.judge = self.judge_patcher.start()
        self.addCleanup(self.judge_patcher.stop)
        self.batch = calendar.CalendarRegistrationBatch("student@school.example", object(), "test-token")

    async def test_same_notice_in_parallel_channels_creates_once(self):
        first = notice()
        second = notice(web_url="https://teams.example/channel-2")
        counts = await asyncio.gather(self.batch.add_notice(first), self.batch.add_notice(second))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 1)

    async def test_distinct_events_on_one_day_share_calendar_lookup(self):
        counts = await asyncio.gather(
            self.batch.add_notice(notice("Physics exam")),
            self.batch.add_notice(notice("Chemistry exam")),
        )
        self.assertEqual(counts, [1, 1])
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 1)

    async def test_different_days_have_separate_calendar_lookups(self):
        await self.batch.add_notice(notice("Physics exam"))
        await self.batch.add_notice(notice(
            "Chemistry exam", start="2026-10-14T00:00:00", end="2026-10-14T23:59:00"
        ))
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 2)

    async def test_failed_calendar_lookup_is_not_cached(self):
        self.get_pages = [response(503), response(200, {"value": []})]
        self.assertEqual(await self.batch.add_notice(notice("Physics exam")), 0)
        self.assertEqual(await self.batch.add_notice(notice("Chemistry exam")), 1)
        self.assertEqual(len([c for c in self.calls if c[0] == "GET"]), 2)
        self.assertTrue(self.batch.had_failures)

    async def test_title_whitespace_and_case_are_normalized(self):
        counts = await asyncio.gather(
            self.batch.add_notice(notice("[Science] Report")),
            self.batch.add_notice(notice("[science]\tReport")),
        )
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.posts), 1)

    async def test_different_titles_are_preserved(self):
        counts = await asyncio.gather(
            self.batch.add_notice(notice("[물리학] 보고서 제출")),
            self.batch.add_notice(notice("[화학] 시험")),
        )
        self.assertEqual(sum(counts), 2)

    async def test_same_title_on_different_dates_is_preserved(self):
        counts = await asyncio.gather(
            self.batch.add_notice(notice()),
            self.batch.add_notice(notice(start="2026-10-14T00:00:00", end="2026-10-14T23:59:00")),
        )
        self.assertEqual(sum(counts), 2)

    async def test_same_title_at_different_times_is_preserved(self):
        counts = await asyncio.gather(
            self.batch.add_notice(notice(start="2026-10-13T09:00:00")),
            self.batch.add_notice(notice(start="2026-10-13T14:00:00")),
        )
        self.assertEqual(sum(counts), 2)

    async def test_failed_creation_does_not_suppress_next_channel(self):
        self.post_statuses = [500, 201]
        counts = await asyncio.gather(self.batch.add_notice(notice()), self.batch.add_notice(notice()))
        self.assertEqual(counts, [0, 1])
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(self.batch.had_failures)

    async def test_long_notice_is_split_once_across_channels(self):
        item = notice(end="2026-10-25T23:59:00")
        counts = await asyncio.gather(self.batch.add_notice(item), self.batch.add_notice(item))
        self.assertEqual(sum(counts), 2)
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(self.posts[0][1]["subject"].endswith("[#시작]"))
        self.assertTrue(self.posts[1][1]["subject"].endswith("[#종료]"))

    async def test_partial_long_notice_failure_can_retry(self):
        self.post_statuses = [201, 500, 201]
        item = notice(end="2026-10-25T23:59:00")
        counts = await asyncio.gather(self.batch.add_notice(item), self.batch.add_notice(item))
        self.assertEqual(counts, [1, 1])
        self.assertEqual(len(self.posts), 3)

    async def test_existing_utc_event_matches_kst_midnight(self):
        self.get_pages = [response(200, {"value": [{
            "subject": "[물리학] 보고서 제출",
            "start": {"dateTime": "2026-10-12T15:00:00", "timeZone": "UTC"},
        }]})]
        self.assertEqual(await self.batch.add_notice(notice()), 0)
        self.assertEqual(self.posts, [])
        kwargs = self.calls[0][2]
        self.assertEqual(kwargs["params"]["startDateTime"], "2026-10-13T00:00:00+09:00")
        self.assertEqual(kwargs["params"]["endDateTime"], "2026-10-14T00:00:00+09:00")
        self.assertEqual(kwargs["headers"]["Prefer"], 'outlook.timezone="Korea Standard Time"')

    async def test_duplicate_on_later_calendar_page_is_found(self):
        next_url = "https://graph.example/next"
        self.get_pages = [
            response(200, {"value": [], "@odata.nextLink": next_url}),
            response(200, {"value": [{
                "subject": "[물리학] 보고서 제출",
                "start": {"dateTime": "2026-10-13T00:00:00", "timeZone": "Korea Standard Time"},
            }]}),
        ]
        self.assertEqual(await self.batch.add_notice(notice()), 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.calls[1][1], next_url)
        self.assertIsNone(self.calls[1][2]["params"])

    async def test_calendar_lookup_failure_does_not_create_blindly(self):
        self.get_pages = [response(503)]
        ok = await calendar.add_notice_to_calendar("student", notice(), object(), "test-token")
        self.assertFalse(ok)
        self.assertEqual(self.posts, [])

    async def test_lookup_failure_marks_batch_for_retry(self):
        self.get_pages = [response(503)]
        self.assertEqual(await self.batch.add_notice(notice()), 0)
        self.assertTrue(self.batch.had_failures)
        self.assertEqual(self.posts, [])

    async def test_users_have_independent_batches(self):
        other = calendar.CalendarRegistrationBatch("other@school.example", object(), "test-token")
        counts = await asyncio.gather(self.batch.add_notice(notice()), other.add_notice(notice()))
        self.assertEqual(counts, [1, 1])
        self.assertNotEqual(self.posts[0][0], self.posts[1][0])

    def test_aware_utc_and_kst_offsets_match(self):
        event = {"subject": "Report", "start": {"dateTime": "2026-10-13T09:00:00+09:00"}}
        existing = {"subject": "Report", "start": {"dateTime": "2026-10-13T00:00:00Z"}}
        self.assertTrue(calendar.is_duplicate_event(event, [existing]))

    def test_empty_titles_do_not_match_every_event(self):
        event = {"subject": "Report", "start": {"dateTime": "2026-10-13T09:00:00"}}
        existing = {"subject": "", "start": {"dateTime": "2026-10-13T09:00:00"}}
        self.assertFalse(calendar.is_duplicate_event(event, [existing]))

    async def test_partial_overlap_across_differently_worded_notices(self):
        async def judge(new, candidates):
            for index, candidate in enumerate(candidates):
                if new["identity_context"] == candidate["identity_context"]:
                    return dedup.MatchDecision(relation="same", matched_index=index, confidence=0.99, reason="fixture match")
            return dedup.MatchDecision(relation="distinct", matched_index=None, confidence=0.99, reason="fixture distinct")
        self.judge.side_effect = judge
        first = [dict(notice(title), identity_context=key) for title, key in [
            ("수학 평가", "A"), ("물리학Ⅰ 중간평가", "B"), ("재량휴업", "C"),
        ]]
        second = [dict(notice(title, web_url="https://teams.example/channel-2"), identity_context=key) for title, key in [
            ("물리1 중간고사 실시", "B"), ("학교장 재량 휴업일", "C"), ("화학 실험 발표", "D"),
        ]]
        counts = await asyncio.gather(
            self.batch.add_notice({"schedules": first}),
            self.batch.add_notice({"schedules": second}),
        )
        self.assertEqual(sum(counts), 4)
        self.assertEqual(len(self.posts), 4)
        self.assertGreater(self.judge.await_count, 0)

    async def test_legacy_calendar_event_with_different_title_is_compared(self):
        event = {
            "subject": "학교장 재량 휴업일",
            "body": {"content": "<p>전교생 휴업</p><a href='https://teams.example'>"+dedup.LEGACY_LINK_LABEL+"</a>"},
            "start": {"dateTime": "2026-10-13T00:00:00", "timeZone": "Korea Standard Time"},
            "end": {"dateTime": "2026-10-13T23:59:00", "timeZone": "Korea Standard Time"},
        }
        self.get_pages = [response(200, {"value": [event]})]
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=0, confidence=0.98, reason="mock same holiday")
        self.assertEqual(await self.batch.add_notice(notice("재량휴업")), 0)
        self.assertEqual(self.posts, [])
        self.judge.assert_awaited_once()

    async def test_date_only_and_timed_description_can_match(self):
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=0, confidence=0.99, reason="mock same meeting")
        first = dict(notice("물리 특강"), time_precision="date")
        second = dict(notice("물리학 초청 강연", start="2026-10-13T14:00:00", end="2026-10-13T15:00:00"), time_precision="time")
        counts = await asyncio.gather(self.batch.add_notice(first), self.batch.add_notice(second))
        self.assertEqual(sum(counts), 1)
        self.judge.assert_awaited_once()

    async def test_explicitly_different_sessions_never_reach_model(self):
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=0, confidence=1, reason="must not be used")
        first = dict(notice("특강 1회", start="2026-10-13T14:00:00", end="2026-10-13T15:00:00"), time_precision="time")
        second = dict(notice("특강 2회", start="2026-10-13T14:10:00", end="2026-10-13T15:10:00"), time_precision="time")
        counts = await asyncio.gather(self.batch.add_notice(first), self.batch.add_notice(second))
        self.assertEqual(sum(counts), 2)
        self.judge.assert_not_awaited()

    async def test_application_deadline_and_event_are_kept_separate(self):
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=0, confidence=1, reason="must not be used")
        first = dict(notice("특강 신청 마감"), event_kind="application_deadline")
        second = dict(notice("특강"), event_kind="event")
        counts = await asyncio.gather(self.batch.add_notice(first), self.batch.add_notice(second))
        self.assertEqual(sum(counts), 2)
        self.judge.assert_not_awaited()

    async def test_distinct_target_grades_are_not_merged(self):
        first = dict(notice("과학 행사"), target_grades=[1])
        second = dict(notice("과학 행사"), target_grades=[2])
        counts = await asyncio.gather(self.batch.add_notice(first), self.batch.add_notice(second))
        self.assertEqual(sum(counts), 2)
        self.judge.assert_not_awaited()

    async def test_identical_titles_with_different_class_context_need_semantics(self):
        first = dict(notice("보고서 제출"), audience="1학년 1반", identity_context="물리학")
        second = dict(notice("보고서 제출"), audience="1학년 2반", identity_context="화학")
        counts = await asyncio.gather(self.batch.add_notice(first), self.batch.add_notice(second))
        self.assertEqual(sum(counts), 2)
        self.judge.assert_awaited_once()

    async def test_uncertain_match_preserves_new_obligation(self):
        self.judge.return_value = dedup.MatchDecision(relation="uncertain", matched_index=None, confidence=0.7, reason="insufficient context")
        counts = await asyncio.gather(self.batch.add_notice(notice("과학 발표")), self.batch.add_notice(notice("탐구 발표")))
        self.assertEqual(sum(counts), 2)

    async def test_low_confidence_same_does_not_drop_event(self):
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=0, confidence=0.6, reason="weak match")
        counts = await asyncio.gather(self.batch.add_notice(notice("과학 발표")), self.batch.add_notice(notice("탐구 발표")))
        self.assertEqual(sum(counts), 2)

    async def test_model_failure_retains_retry_state_and_does_not_post(self):
        await self.batch.add_notice(notice("과학 발표"))
        self.judge.side_effect = RuntimeError("mock API timeout")
        with self.assertRaises(RuntimeError):
            await self.batch.add_notice(notice("탐구 발표"))
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.batch.had_failures)

    async def test_invalid_candidate_index_is_retried(self):
        await self.batch.add_notice(notice("과학 발표"))
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=9, confidence=1, reason="invalid response")
        with self.assertRaises(ValueError):
            await self.batch.add_notice(notice("탐구 발표"))
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.batch.had_failures)

    async def test_private_calendar_details_are_not_sent_to_model(self):
        self.get_pages = [response(200, {"value": [{
            "subject": "개인 상담",
            "body": {"content": "Private details"},
            "start": {"dateTime": "2026-10-13T00:00:00"},
            "end": {"dateTime": "2026-10-13T23:59:00"},
        }]})]
        self.assertEqual(await self.batch.add_notice(notice("학교 행사")), 1)
        self.judge.assert_not_awaited()

    async def test_metadata_survives_calendar_round_trip(self):
        first = dict(notice("재량 휴업"), event_kind="holiday", time_precision="date", audience="전교생", identity_context="10월 휴업")
        await self.batch.add_notice(first)
        stored = self.posts[0][1]
        self.assertEqual(dedup.metadata(stored)["identity_context"], "10월 휴업")
        self.get_pages = [response(200, {"value": [stored]})]
        later_batch = calendar.CalendarRegistrationBatch("student@school.example", object(), "test-token")
        self.judge.return_value = dedup.MatchDecision(relation="same", matched_index=0, confidence=0.99, reason="mock same holiday")
        self.assertEqual(await later_batch.add_notice(dict(notice("학교장 재량휴업일"), event_kind="holiday")), 0)
        self.assertEqual(len(self.posts), 1)

    async def test_candidate_chunks_do_not_discard_late_matches(self):
        existing = []
        for i in range(14):
            existing.append(calendar.make_event_body(notice("행사 "+str(i))))
        self.judge.side_effect = [
            dedup.MatchDecision(relation="distinct", matched_index=None, confidence=1, reason="first chunk"),
            dedup.MatchDecision(relation="same", matched_index=1, confidence=0.99, reason="late match"),
        ]
        result = await dedup.find_duplicate(calendar.make_event_body(notice("별도 표현")), existing)
        self.assertIs(result, existing[13])
        self.assertEqual(self.judge.await_count, 2)



if __name__ == "__main__":
    unittest.main()
