from __future__ import annotations

"""In-process, non-durable Bitrix24 webhook dedup storage.

This deduper suppresses repeated Bitrix24 webhook event IDs only within the
current Python process. It intentionally does not survive interpreter restart,
and cross-process idempotency should move to a later durable store if webhook
handling becomes side-effectful.
"""

from collections import OrderedDict
from collections.abc import Callable
import time

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_EVENTS = 10000


class InProcessEventDeduper:
    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_events: int = DEFAULT_MAX_EVENTS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = max(float(ttl_seconds), 0.0)
        self._max_events = max(int(max_events), 1)
        self._clock = clock or time.monotonic
        self._seen_event_ids: OrderedDict[str, float] = OrderedDict()

    def _purge_expired(self, now: float | None = None) -> None:
        current_time = self._clock() if now is None else now
        expired_event_ids = [
            event_id for event_id, expires_at in self._seen_event_ids.items() if expires_at <= current_time
        ]
        for event_id in expired_event_ids:
            self._seen_event_ids.pop(event_id, None)

    def is_duplicate(self, event_id: str) -> bool:
        self._purge_expired()
        return event_id in self._seen_event_ids

    def remember(self, event_id: str) -> None:
        now = self._clock()
        self._purge_expired(now)
        expires_at = now + self._ttl_seconds
        self._seen_event_ids.pop(event_id, None)
        self._seen_event_ids[event_id] = expires_at
        while len(self._seen_event_ids) > self._max_events:
            self._seen_event_ids.popitem(last=False)


_PROCESS_SHARED_EVENT_DEDUPER = InProcessEventDeduper()


def get_process_shared_event_deduper() -> InProcessEventDeduper:
    return _PROCESS_SHARED_EVENT_DEDUPER
