"""Parse a basic Graph notification path against a trusted subscription.

Only identifiers are returned. A caller must recheck delegated access and build
its own Graph URL; this parser never makes a payload URL safe to forward.
"""

import re
from urllib.parse import unquote


_IDENTIFIER = re.compile(r"[A-Za-z0-9._~:@+=-]{1,200}", re.ASCII)
_ODATA = re.compile(
    r"teams\('([^']{1,600})'\)/channels\('([^']{1,600})'\)"
    r"/messages\('([^']{1,600})'\)(?:/replies\('([^']{1,600})'\))?"
)


def _identifier(encoded):
    try:
        value = unquote(encoded, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    # The finite alphabet also rejects encoded separators, controls, percent
    # escapes left after one decode, OData quoting, and URI authority syntax.
    if value in (".", "..") or _IDENTIFIER.fullmatch(value) is None:
        return None
    return value


def parse_notification_target(resource, team_id, channel_id):
    """Return message/reply IDs only for this subscription's strict path.

    Accept slash paths and Graph's quoted OData path form, with at most one
    leading slash. Full URLs, query strings, fragments, other teams/channels,
    and unrecognized resource shapes are rejected with ``None``.
    """
    if (
        not isinstance(resource, str)
        or not 0 < len(resource) <= 4096
        or not isinstance(team_id, str)
        or not isinstance(channel_id, str)
        or not team_id
        or not channel_id
    ):
        return None
    if any(ord(char) < 33 or ord(char) > 126 for char in resource):
        return None
    path = resource[1:] if resource.startswith("/") else resource
    match = _ODATA.fullmatch(path)
    if match:
        parts = list(match.groups())
    else:
        segments = path.split("/")
        if len(segments) not in (6, 8):
            return None
        if segments[0::2] != (
            ["teams", "channels", "messages"]
            if len(segments) == 6
            else ["teams", "channels", "messages", "replies"]
        ):
            return None
        parts = segments[1::2]
        if len(parts) == 3:
            parts.append(None)
    decoded = [_identifier(value) if value is not None else None for value in parts]
    if (
        any(value is None for value in decoded[:3])
        or (parts[3] is not None and decoded[3] is None)
        or decoded[0] != team_id
        or decoded[1] != channel_id
    ):
        return None
    return {"message_id": decoded[2], "reply_id": decoded[3]}
