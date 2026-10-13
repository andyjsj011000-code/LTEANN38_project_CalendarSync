"""Share a source message's user-independent extraction within one process."""
import asyncio
import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future

_results: OrderedDict[str, tuple[float, object]] = OrderedDict()
_in_flight: dict[str, Future] = {}
_guard = threading.Lock()
_ttl_seconds = 6 * 60 * 60
_max_entries = 1024


def message_cache_key(team_id: str, channel_id: str, message: dict) -> str:
    """Bind cached results to both the source channel and the full message version."""
    source = json.dumps(
        [team_id, channel_id, message], sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def named_for_user(target_people: list[str], user_name: str | None) -> bool:
    """An explicit individual audience is not a general channel announcement."""
    if not target_people:
        return True
    if not user_name:
        return False
    normalize = lambda name: "".join(name.split()).casefold()
    return normalize(user_name) in {normalize(name) for name in target_people}


async def cached_extraction(key: str, producer):
    """Coalesce concurrent requests, including requests from separate event loops."""
    with _guard:
        entry = _results.get(key)
        if entry is not None:
            expires_at, cached = entry
            if expires_at > time.monotonic():
                _results.move_to_end(key)
                return copy.deepcopy(cached)
            del _results[key]
        pending = _in_flight.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            _in_flight[key] = pending

    if not owner:
        return copy.deepcopy(await asyncio.shield(asyncio.wrap_future(pending)))

    try:
        result = await producer()
        if result is None:
            raise RuntimeError("Schedule extraction returned no result")
    except BaseException as exc:
        with _guard:
            del _in_flight[key]
        pending.set_exception(exc)
        raise
    else:
        with _guard:
            _results[key] = (time.monotonic() + _ttl_seconds, copy.deepcopy(result))
            if len(_results) > _max_entries:
                _results.popitem(last=False)
            del _in_flight[key]
        pending.set_result(result)
        return copy.deepcopy(result)
