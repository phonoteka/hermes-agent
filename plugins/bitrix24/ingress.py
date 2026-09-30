from __future__ import annotations

import json
import os
from typing import Any, Callable

from .client import Bitrix24Client, _validated_webhook_base_url
from .enrichment import (
    Bitrix24EventSource, Bitrix24SourceUnavailable, RestBitrix24EventSource,
    bot_user_id_from_webhook_url,
)
from .dedup import InProcessEventDeduper, get_process_shared_event_deduper
from .events import (
    _SUPPORTED_EVENTS,
    _TASK_EVENTS,
    _COMMENT_EVENTS,
    _configured_application_token,
    _configured_domain,
    _event_id,
    _normalize_supported_event,
    _payload_application_token,
    _payload_domain,
    _supported_event,
)


def _has_valid_webhook_url() -> bool:
    try:
        _validated_webhook_base_url(os.getenv("BITRIX24_WEBHOOK_URL", ""))
    except RuntimeError:
        return False
    return True


def _has_application_token() -> bool:
    return bool(os.getenv("BITRIX24_APPLICATION_TOKEN", "").strip())


class _OutboundWebhookCheck:
    def __call__(self) -> bool:
        return _has_valid_webhook_url()


class _IngressWebhookCheck:
    def __call__(self) -> bool:
        return _has_valid_webhook_url() and _has_application_token()


def _make_webhook_ingress_handler(
    *,
    materialize_attachments: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    agent_request_from_result: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    deduper: InProcessEventDeduper | None = None,
    event_source: Bitrix24EventSource | None = None,
    bot_user_id: str | None = None,
):
    """Authenticate before lazy enrichment; remember only accepted events, never source failures."""
    event_deduper = deduper or get_process_shared_event_deduper()

    def _handle_bitrix24_webhook_ingress(args: dict[str, Any] | None = None, **_: Any) -> str:
        nonlocal event_source, bot_user_id
        payload = args if isinstance(args, dict) else {}
        event = _supported_event(payload)
        event_id = _event_id(payload)
        expected_application_token = _configured_application_token()
        received_application_token = _payload_application_token(payload)
        configured_domain = _configured_domain()
        source_domain = _payload_domain(payload)

        if not payload or not event:
            return json.dumps({"accepted": False, "reason": "invalid_payload"})

        if not _has_valid_webhook_url():
            return json.dumps({"accepted": False, "reason": "invalid_webhook_url", "event": event})

        if not configured_domain:
            return json.dumps({"accepted": False, "reason": "invalid_webhook_url", "event": event})

        if not expected_application_token:
            return json.dumps(
                {"accepted": False, "reason": "webhook_auth_not_configured", "event": event}
            )

        if not received_application_token or received_application_token != expected_application_token:
            return json.dumps(
                {"accepted": False, "reason": "unauthorized_application_token", "event": event}
            )

        if source_domain != configured_domain:
            return json.dumps(
                {
                    "accepted": False,
                    "reason": "unauthorized_source",
                    "event": event,
                    "source_domain": source_domain,
                }
            )

        if event not in _SUPPORTED_EVENTS:
            return json.dumps({"accepted": False, "reason": "unsupported_event", "event": event})

        if not event_id:
            return json.dumps({"accepted": False, "reason": "unknown_payload_shape", "event": event})

        if event_deduper.is_duplicate(event_id):
            return json.dumps(
                {
                    "accepted": False,
                    "reason": "duplicate_event",
                    "event": event,
                    "event_id": event_id,
                    "deduped": True,
                }
            )

        try:
            if event in (_TASK_EVENTS | _COMMENT_EVENTS):
                if event_source is None:
                    event_source = RestBitrix24EventSource(Bitrix24Client())
                if bot_user_id is None:
                    bot_user_id = bot_user_id_from_webhook_url(os.getenv("BITRIX24_WEBHOOK_URL", ""))
            normalized = _normalize_supported_event(
                payload, event=event, event_id=event_id, source_domain=source_domain,
                event_source=event_source, bot_user_id=bot_user_id or "",
            )
        except Bitrix24SourceUnavailable:
            return json.dumps({"accepted": False, "reason": "source_unavailable"})
        if normalized is None:
            return json.dumps(
                {
                    "accepted": False,
                    "reason": "unknown_payload_shape",
                    "event": event,
                    "event_id": event_id,
                }
            )

        if normalized.get("accepted") is True:
            if materialize_attachments is not None:
                attachments = materialize_attachments(normalized)
                if attachments:
                    normalized["attachments"] = attachments
            if agent_request_from_result is not None:
                agent_request = agent_request_from_result(normalized)
                if agent_request is not None:
                    normalized["agent_request"] = agent_request
                    tool_candidates = agent_request.get("tool_candidates")
                    if isinstance(tool_candidates, list) and tool_candidates:
                        normalized["deferred_actions"] = tool_candidates
            event_deduper.remember(event_id)
        return json.dumps(normalized)

    return _handle_bitrix24_webhook_ingress
