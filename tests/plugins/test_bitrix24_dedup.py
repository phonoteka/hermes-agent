from __future__ import annotations

import json

import pytest

@pytest.fixture(autouse=True)
def fake_event_source(monkeypatch):
    from plugins.bitrix24 import ingress
    from tests.plugins.test_bitrix24_event_enrichment import FakeEventSource

    source = FakeEventSource('@hermes take a look')
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    monkeypatch.setenv("BITRIX24_ALLOWED_GROUP_IDS", "52,54")
    return source


from plugins.bitrix24.dedup import InProcessEventDeduper
from plugins.bitrix24.ingress import _make_webhook_ingress_handler


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _task_payload(*, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "event": "ONTASKCOMMENTADD",
        "auth": {"domain": "example.com", "application_token": "expected-app-token"},
        "data": {"FIELDS_AFTER": {"ID": "0", "TASK_ID": "42", "MESSAGE_ID": "84"}},
    }


def test_in_process_event_deduper_expires_entries_with_fake_clock():
    clock = _FakeClock()
    deduper = InProcessEventDeduper(ttl_seconds=10, max_events=10, clock=clock)

    deduper.remember("evt-1")

    assert deduper.is_duplicate("evt-1") is True

    clock.advance(10)

    assert deduper.is_duplicate("evt-1") is False
    assert list(deduper._seen_event_ids) == []


def test_in_process_event_deduper_evicts_oldest_entries_over_max_size():
    deduper = InProcessEventDeduper(ttl_seconds=100, max_events=2, clock=_FakeClock())

    deduper.remember("evt-1")
    deduper.remember("evt-2")
    deduper.remember("evt-3")

    assert deduper.is_duplicate("evt-1") is False
    assert deduper.is_duplicate("evt-2") is True
    assert deduper.is_duplicate("evt-3") is True
    assert list(deduper._seen_event_ids) == ["evt-2", "evt-3"]


def test_webhook_ingress_accepts_same_event_again_after_ttl_expiry(monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@hermes")

    clock = _FakeClock()
    deduper = InProcessEventDeduper(ttl_seconds=5, max_events=10, clock=clock)
    handler = _make_webhook_ingress_handler(deduper=deduper)
    payload = _task_payload(event_id="evt-ttl-1")

    first = json.loads(handler(payload, task_id="build-fix"))
    second = json.loads(handler(payload, task_id="build-fix"))

    clock.advance(5)

    third = json.loads(handler(payload, task_id="build-fix"))

    assert first["accepted"] is True
    assert second == {
        "accepted": False,
        "reason": "duplicate_event",
        "event": "ONTASKCOMMENTADD",
        "event_id": "evt-ttl-1",
        "deduped": True,
    }
    assert third["accepted"] is True
    assert third["event_id"] == "evt-ttl-1"
    assert third["deduped"] is False
