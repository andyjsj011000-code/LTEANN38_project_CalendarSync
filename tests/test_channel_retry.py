"""A partial Teams fetch must not be mistaken for a complete empty channel."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

import Channel_Export as channels


class ChannelRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_thread_with_new_reply_does_not_hide_new_root(self):
        first = httpx.Response(200, json={
            "value": [{"id": "old-root", "createdDateTime": "2026-10-01T01:00:00Z",
                       "lastModifiedDateTime": "2026-10-13T02:00:00Z"}],
            "@odata.nextLink": "https://graph.example/next",
        }, request=httpx.Request("GET", "https://graph.example/messages"))
        second = httpx.Response(200, json={
            "value": [{"id": "new-root", "createdDateTime": "2026-10-12T01:00:00Z",
                       "lastModifiedDateTime": "2026-10-12T01:00:00Z"}],
        }, request=httpx.Request("GET", "https://graph.example/next"))
        with patch.object(channels, "safe_http_request", new=AsyncMock(side_effect=[first, second])):
            result = await channels.channel_export(
                "team", "channel", datetime(2026, 10, 11, tzinfo=timezone.utc),
                object(), "token",
            )
        self.assertEqual([item["id"] for item in result], ["new-root"])

    async def test_failed_later_page_propagates_after_first_page(self):
        first = httpx.Response(200, json={
            "value": [{"createdDateTime": "2026-10-13T01:00:00Z", "id": "first"}],
            "@odata.nextLink": "https://graph.example/next",
        }, request=httpx.Request("GET", "https://graph.example/first"))
        second = httpx.Response(503, json={}, request=httpx.Request("GET", "https://graph.example/next"))
        with patch.object(channels, "safe_http_request", new=AsyncMock(side_effect=[first, second])):
            with self.assertRaises(httpx.HTTPStatusError):
                await channels.channel_export(
                    "team", "channel", datetime(2026, 10, 12, tzinfo=timezone.utc),
                    object(), "token",
                )

    async def test_missing_token_propagates(self):
        with patch.object(channels, "get_graph_access_token", new=AsyncMock(return_value=None)):
            with self.assertRaises(RuntimeError):
                await channels.channel_export(
                    "team", "channel", datetime(2026, 10, 12, tzinfo=timezone.utc), object(),
                )
