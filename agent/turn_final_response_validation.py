"""Host validation for candidate final responses.

The conversation loop owns retry/persistence policy; this module owns the bounded,
side-effect-free contract for validator results and feedback payloads.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Dict, List, Optional

_DEFAULT_FINAL_RESPONSE_VALIDATION_FEEDBACK = (
    "Your previous final response was rejected by the host validator. "
    "Please correct it and try again."
)
_VALIDATION_FEEDBACK_MAX_BYTES = 4096
_FAILURE_MESSAGE_MAX_BYTES = 500


def _normalize_final_response_validation_result(result: Any) -> dict:
    """Normalize a generic external validator result for the host loop."""
    if result is None:
        return {"accepted": True, "retryable": False, "feedback_message": ""}
    if isinstance(result, bool):
        return {
            "accepted": result,
            "retryable": not result,
            "feedback_message": "" if result else _DEFAULT_FINAL_RESPONSE_VALIDATION_FEEDBACK,
        }
    if isinstance(result, str):
        return {
            "accepted": False,
            "retryable": True,
            "feedback_message": result or _DEFAULT_FINAL_RESPONSE_VALIDATION_FEEDBACK,
        }
    if isinstance(result, dict):
        accepted = result.get("accepted")
        if not isinstance(accepted, bool):
            raise TypeError("final_response_validator accepted must be boolean")
        retryable = result.get("retryable", not accepted)
        if not isinstance(retryable, bool):
            raise TypeError("final_response_validator retryable must be boolean")
        feedback = result.get("feedback")
        return {
            "accepted": accepted,
            "retryable": retryable,
            "feedback_message": str(
                result.get("feedback_message")
                or (feedback if isinstance(feedback, str) else None)
                or ("" if accepted else _DEFAULT_FINAL_RESPONSE_VALIDATION_FEEDBACK)
            ),
            "feedback": feedback,
        }

    accepted = getattr(result, "accepted", None)
    if accepted is not None:
        if not isinstance(accepted, bool):
            raise TypeError("final_response_validator accepted must be boolean")
        retryable = getattr(result, "retryable", not accepted)
        if not isinstance(retryable, bool):
            raise TypeError("final_response_validator retryable must be boolean")
        feedback = getattr(result, "feedback", None)
        return {
            "accepted": accepted,
            "retryable": retryable,
            "feedback_message": str(
                getattr(result, "feedback_message", None)
                or (feedback if isinstance(feedback, str) else None)
                or ("" if accepted else _DEFAULT_FINAL_RESPONSE_VALIDATION_FEEDBACK)
            ),
            "feedback": feedback,
        }

    raise TypeError(
        "final_response_validator must return None, bool, str, or an object/dict "
        "with accepted/retryable/feedback_message fields"
    )


def _bounded_failure_text(value: Any, *, limit: int = _FAILURE_MESSAGE_MAX_BYTES) -> str:
    """Bound validator diagnostics and redact common credential forms."""
    text = str(value or "").replace("\x00", " ").strip()
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_ -]?key\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _detach_validation_feedback(value: Any) -> Any:
    """Copy JSON feedback and enforce the bounded host-to-model contract."""
    def _detach(item: Any, *, ancestors: set[int]) -> Any:
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int) and not isinstance(item, bool):
            return item
        if isinstance(item, float):
            if item != item or abs(item) == float("inf"):
                raise ValueError("validationFeedback must contain finite JSON numbers")
            return item
        if isinstance(item, str):
            return item
        if isinstance(item, list):
            marker = id(item)
            if marker in ancestors:
                raise ValueError("validationFeedback must not contain cycles")
            return [_detach(child, ancestors=ancestors | {marker}) for child in item]
        if isinstance(item, dict):
            marker = id(item)
            if marker in ancestors:
                raise ValueError("validationFeedback must not contain cycles")
            detached = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("validationFeedback object keys must be strings")
                detached[key] = _detach(child, ancestors=ancestors | {marker})
            return detached
        raise ValueError("validationFeedback must be JSON-compatible")

    detached = _detach(value, ancestors=set())
    try:
        encoded = json.dumps(
            detached, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError("validationFeedback must be JSON-compatible") from exc
    if len(encoded) > _VALIDATION_FEEDBACK_MAX_BYTES:
        raise ValueError("validationFeedback exceeds the 4096-byte limit")
    return detached


def validation_metadata(
    messages: List[Dict[str, Any]], *, api_call_count: int, finish_reason: Any,
    agent: Any, effective_task_id: Any, validation_attempt: int,
) -> Dict[str, Any]:
    """Build validator input without exposing mutable transcript objects."""
    return {
        "messages": copy.deepcopy(messages),
        "api_call_count": api_call_count,
        "finish_reason": finish_reason,
        "model": agent.model,
        "provider": agent.provider,
        "session_id": agent.session_id or "",
        "task_id": effective_task_id,
        "validation_attempt": validation_attempt,
    }


def validate_candidate(
    validator: Callable[[str, Dict[str, Any]], Any], candidate: str, metadata: Dict[str, Any],
) -> dict:
    """Invoke and normalize one host validator result."""
    return _normalize_final_response_validation_result(validator(candidate, metadata))


__all__ = [
    "_DEFAULT_FINAL_RESPONSE_VALIDATION_FEEDBACK",
    "_bounded_failure_text",
    "_detach_validation_feedback",
    "_normalize_final_response_validation_result",
    "validate_candidate",
    "validation_metadata",
]
