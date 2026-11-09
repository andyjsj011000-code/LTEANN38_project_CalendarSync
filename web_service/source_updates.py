"""Apply source updates while retaining personal edits and explicit tombstones."""


def automatic_source_update(current, seen_hash, incoming, fingerprint):
    if current is not None and fingerprint(current) == seen_hash:
        return {**incoming, "_source_cancelled": incoming["cancelled"]}, True
    if current is not None and (incoming["cancelled"] or current.get("_source_cancelled")):
        # Cancellation/deletion is authoritative even for a locally edited item.
        # Keep a review for the other conflicting fields and preserve local state.
        return {
            **current,
            "cancelled": incoming["cancelled"],
            "_source_cancelled": incoming["cancelled"],
        }, False
    return None, False
