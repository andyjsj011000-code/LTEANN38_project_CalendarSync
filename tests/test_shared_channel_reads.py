"""One Teams read may feed several independent user checkpoints."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from shared_channel_reads import SharedChannelReads, messages_since


UTC = timezone.utc
NOW = datetime(2026, 10, 13, 8, tzinfo=UTC)
CHANNEL = {"team_id": "team", "channel_id": "common"}


def message(name, hours_ago):
    return {
        "id": name,
        "createdDateTime": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


class SharedChannelReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_fetch_uses_oldest_checkpoint_but_filters_per_user(self):
        calls = []
        messages = [message("recent", 1), message("earlier", 4)]

        async def fetch(team, channel, checkpoint):
            calls.append((team, channel, checkpoint))
            await asyncio.sleep(0)
            return messages

        checkpoints = {
            "alice": NOW - timedelta(hours=2),
            "bob": NOW - timedelta(hours=5),
        }
        reads = SharedChannelReads(
            {"alice": [CHANNEL], "bob": [CHANNEL]}, checkpoints, fetch
        )
        results = await asyncio.gather(*(
            reads.get(user, "team", "common", checkpoints[user])
            for user in checkpoints
        ))
        self.assertEqual(calls, [("team", "common", checkpoints["bob"])])
        self.assertEqual(
            [m["id"] for m in messages_since(results[0], checkpoints["alice"])],
            ["recent"],
        )
        self.assertEqual(
            [m["id"] for m in messages_since(results[1], checkpoints["bob"])],
            ["recent", "earlier"],
        )

    async def test_lagging_user_has_a_separate_read_group(self):
        calls = []

        async def fetch(team, channel, checkpoint):
            calls.append(checkpoint)
            return []

        checkpoints = {
            "alice": NOW - timedelta(minutes=20),
            "bob": NOW - timedelta(minutes=40),
            "lagging": NOW - timedelta(days=7),
        }
        reads = SharedChannelReads(
            {user: [CHANNEL] for user in checkpoints}, checkpoints, fetch
        )
        await asyncio.gather(*(
            reads.get(user, "team", "common", checkpoints[user])
            for user in checkpoints
        ))
        self.assertEqual(reads.read_count, 2)
        self.assertCountEqual(calls, [checkpoints["bob"], checkpoints["lagging"]])

    async def test_failed_shared_read_reaches_every_user_for_retry(self):
        calls = 0

        async def fetch(team, channel, checkpoint):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            raise RuntimeError("Graph read failed")

        checkpoint = NOW - timedelta(hours=1)
        reads = SharedChannelReads(
            {"alice": [CHANNEL], "bob": [CHANNEL]},
            {"alice": checkpoint, "bob": checkpoint}, fetch,
        )
        results = await asyncio.gather(
            reads.get("alice", "team", "common", checkpoint),
            reads.get("bob", "team", "common", checkpoint),
            return_exceptions=True,
        )
        self.assertEqual(calls, 1)
        self.assertTrue(all(isinstance(item, RuntimeError) for item in results))

    async def test_checkpoint_moved_back_falls_back_to_individual_read(self):
        checkpoint = NOW - timedelta(hours=1)

        async def fetch(team, channel, since):
            self.fail("An insufficient shared read must not be used")

        reads = SharedChannelReads(
            {"alice": [CHANNEL]}, {"alice": checkpoint}, fetch
        )
        result = await reads.get(
            "alice", "team", "common", checkpoint - timedelta(minutes=1)
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
