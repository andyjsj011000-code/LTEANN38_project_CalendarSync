"""Credential-generation validation shared by non-HTTP workflows."""

from .collection_types import DeviceSnapshot
from .ports import ConnectionStore, TokenProvider
from .school_graph import GraphFailure


# 토큰 갱신 후 장치 정보를 다시 읽어 이후 저장 검사가 최신 자격증명 세대를 사용하게 한다.
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
