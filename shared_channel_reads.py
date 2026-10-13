"""Share Teams channel reads within one polling round.

Each user still applies their own checkpoint and writes to their own calendar.
Users whose checkpoints differ by more than a day are placed in separate
read groups, so an old checkpoint does not delay recently synced users.
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta


def messages_since(messages: list[dict], checkpoint: datetime) -> list[dict]:
    """Select newly created root messages for one user's checkpoint."""
    return [
        message for message in messages
        if message.get("createdDateTime")
        and datetime.fromisoformat(
            message["createdDateTime"].replace("Z", "+00:00")
        ) > checkpoint
    ]


class SharedChannelReads:
    def __init__(
        self,
        channels_by_user: dict[str, list[dict]],
        checkpoints: dict[str, datetime],
        fetch,
        max_spread: timedelta = timedelta(days=1),
    ):
        if max_spread < timedelta(0):
            raise ValueError("max_spread must be nonnegative")
        self._fetch = fetch
        self._memberships = {}
        self._cutoffs = {}
        self._tasks = {}

        by_channel = defaultdict(list)
        for user_id, channels in channels_by_user.items():
            checkpoint = checkpoints[user_id]
            for channel in channels:
                identity = (channel["team_id"], channel["channel_id"])
                by_channel[identity].append((user_id, checkpoint))

        for (team_id, channel_id), members in by_channel.items():
            # The most recent checkpoint anchors each group. A lagging member
            # never makes a newer group fetch beyond max_spread.
            members.sort(key=lambda item: item[1], reverse=True)
            newest = None
            group = -1
            for user_id, checkpoint in members:
                if newest is None or newest - checkpoint > max_spread:
                    newest = checkpoint
                    group += 1
                key = (team_id, channel_id, group)
                self._memberships[(user_id, team_id, channel_id)] = key
                self._cutoffs[key] = checkpoint

    @property
    def read_count(self) -> int:
        return len(self._cutoffs)

    async def get(
        self, user_id: str, team_id: str, channel_id: str,
        checkpoint: datetime,
    ) -> list[dict] | None:
        """Return shared messages, or None if this user's checkpoint moved back."""
        key = self._memberships.get((user_id, team_id, channel_id))
        if key is None or checkpoint < self._cutoffs[key]:
            return None
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(
                self._fetch(key[0], key[1], self._cutoffs[key])
            )
        return await asyncio.shield(self._tasks[key])
