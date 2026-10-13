import asyncio
import unittest

from message_analysis_cache import cached_extraction, message_cache_key, named_for_user


class SharedExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_source_is_analyzed_once_for_concurrent_users(self):
        key = message_cache_key("team", "channel", {"id": self.id(), "body": "exam"})
        calls = 0

        async def analyze():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"schedules": [{"title": "exam"}]}

        first, second = await asyncio.gather(
            cached_extraction(key, analyze), cached_extraction(key, analyze)
        )
        first["schedules"][0]["title"] = "changed for one user"
        third = await cached_extraction(key, analyze)
        self.assertEqual(calls, 1)
        self.assertEqual(second["schedules"][0]["title"], "exam")
        self.assertEqual(third["schedules"][0]["title"], "exam")

    async def test_failure_is_not_cached(self):
        key = message_cache_key("team", "channel", {"id": self.id()})
        calls = 0

        async def analyze():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary model error")
            return {"schedules": []}

        with self.assertRaises(RuntimeError):
            await cached_extraction(key, analyze)
        self.assertEqual(await cached_extraction(key, analyze), {"schedules": []})
        self.assertEqual(calls, 2)

    def test_source_or_edit_changes_cache_identity(self):
        message = {"id": "post", "body": {"content": "first"}}
        original = message_cache_key("team", "channel", message)
        self.assertNotEqual(original, message_cache_key("team", "other", message))
        message["body"]["content"] = "edited"
        self.assertNotEqual(original, message_cache_key("team", "channel", message))

    def test_named_notice_is_filtered_after_shared_extraction(self):
        self.assertTrue(named_for_user([], "정서진"))
        self.assertTrue(named_for_user(["정 서진"], "정서진"))
        self.assertFalse(named_for_user(["김민수"], "정서진"))
        self.assertFalse(named_for_user(["정서진"], None))
