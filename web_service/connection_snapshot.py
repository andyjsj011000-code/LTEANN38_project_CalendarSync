"""Credential-generation validation shared by non-HTTP workflows."""

from .collection_types import DeviceSnapshot
from .ports import ConnectionStore, TokenProvider
from .school_graph import GraphFailure


def acquire_snapshot(
    tokens: TokenProvider, device: DeviceSnapshot, db: ConnectionStore
) -> tuple[str, DeviceSnapshot]:
    """Use the current credential generation after a possible token refresh."""
    token = tokens.acquire(device)
    current = db.device(device["id_hash"])
    if (
        not current
        or current["user_id"] != device["user_id"]
        or current["tenant_id"] != device["tenant_id"]
        or current.get("state") == "reauth_required"
    ):
        raise GraphFailure("reauth_required")
    return token, current
