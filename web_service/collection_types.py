"""Internal collection records, using the existing dictionary/JSON shapes.

These types describe data after boundary checks; they do not validate Graph
responses or grant access. Keep the runtime checks at each trust boundary.
"""

from typing import Literal, NotRequired, TypedDict

EventKind = Literal["assignment", "application", "event"]


class Channel(TypedDict):
    id: str
    team_id: str
    name: NotRequired[str]
    tenant_id: NotRequired[str]


class EventFields(TypedDict):
    title: str
    notes: str
    start_date: str
    end_date: str
    time: str | None
    kind: EventKind
    cancelled: bool


class EventDraft(EventFields):
    """Validated AI output; dates and times are already JSON strings."""

    key: str


class SourceEvent(EventFields):
    id: str
    channel_id: str
    key: NotRequired[str]
    source_url: NotRequired[str]
    source_version: NotRequired[str]


class RetiredEvent(TypedDict):
    """Identity hints only. Retention deliberately removes notice content."""

    id: str
    end_date: str
    key: NotRequired[str | None]
    source_version: NotRequired[str | None]
    source_hash: NotRequired[str]


class CachedAnalysis(TypedDict):
    version: str
    events: list[SourceEvent]
    retired: NotRequired[list[RetiredEvent]]


CollectionCounter = Literal[
    "channels_total",
    "channels_scanned",
    "messages_seen",
    "messages_analyzed",
    "messages_cached",
    "messages_failed",
]


class CollectionReport(TypedDict):
    channels_total: int
    channels_scanned: int
    messages_seen: int
    messages_analyzed: int
    messages_cached: int
    messages_failed: int
    partial: bool
    # Present only when a progress callback was requested.
    channels_checked: NotRequired[int]


class DeviceIdentity(TypedDict):
    id_hash: str
    user_id: str
    tenant_id: str
    object_id: str
    version: int


class DeviceSnapshot(DeviceIdentity):
    """Fields consumed by token acquisition; the stored row has more columns."""

    credential_blob: str
    token_expires: int
    background: bool | int
    state: str


class PreparedCollection(TypedDict):
    """Token-free handoff; reuse never extends the original permission expiry."""

    device: DeviceIdentity
    channels: list[Channel]
    valid_until: float


class PermissionSnapshot(TypedDict):
    state: str
    checked_at: int
    channels: frozenset[str]


class PermissionReadiness(TypedDict):
    state: str
    verified: bool
    retryable: bool
    channels: frozenset[str]


class CredentialPayload(TypedDict, total=False):
    token: str
    cache: str
    assignment_access: bool
