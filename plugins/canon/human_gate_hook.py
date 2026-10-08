"""Deliver Canon pause notifications to the bound Hermes profile session."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import sys
from typing import Any
from urllib.parse import quote

import profile_runtime  # Load this checkout's profile environment before config.
from hermes_cli.config import load_config, require_parseable_user_config


_IDENTITY_KEYS = ("runId", "threadId", "checkpointId", "nodeId")
_PROMPT_KEYS = ("gateId", "subjectRevision", "subjectDigest", "decisionAttempt", "actions")


def _wake_text(notification: dict[str, Any]) -> str:
    """Pre: notification follows Canon's human-gate schema. Post: preserves card bytes and appends compact pause identity and delivery instruction. Raises: KeyError/TypeError for malformed input."""
    prompt = notification["humanGatePrompt"]
    identity = {**{key: notification[key] for key in _IDENTITY_KEYS},
                **{key: prompt[key] for key in _PROMPT_KEYS}}
    delivery = notification.get("humanGateDelivery", "не указан: спроси пользователя этой сессии")
    return ("Пауза Canon: требуется решение пользователя\n"
            f"Доставка: {delivery}\n\n" + prompt["cardBody"] + "\n"
            + json.dumps(identity, ensure_ascii=False, separators=(",", ":")))


def _send_wake(notification: dict[str, Any], config: dict[str, Any]) -> int:
    """Pre: notification binds hostSession; config contains profile webhook host, port and route secret. Post: sends one signed JSON POST, closes connection and returns HTTP status without exposing response body. Raises: KeyError/ValueError/TypeError or transport errors; no retry."""
    route = config.get("plugins", {}).get("entries", {}).get("canon", {}).get("wake_route", "canon-human-gate")
    extra = config["platforms"]["webhook"]["extra"]
    secret = extra["routes"][route]["secret"]
    if not isinstance(secret, str) or not secret or "${" in secret:
        raise ValueError("route secret is unresolved")
    body = json.dumps({"session_key": notification["hostSession"], "text": _wake_text(notification)},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest(),
        "X-Request-ID": f"{notification['checkpointId']}:{notification['humanGatePrompt']['decisionAttempt']}",
    }
    connection = http.client.HTTPConnection(extra["host"], int(extra["port"]), timeout=20)
    try:
        connection.request("POST", "/webhooks/" + quote(route, safe=""), body=body, headers=headers)
        response = connection.getresponse()
        try:
            return response.status
        finally:
            response.close()
    finally:
        connection.close()


def main() -> int:
    """Pre: stdin is a Canon notification and HERMES_HOME selects its profile. Post: exits 2 without I/O for absent hostSession, 0 only for HTTP 202, otherwise 1 with body/secret-free stderr. Raises: none for ordinary input/config/transport failures."""
    try:
        notification = json.load(sys.stdin)
        if not notification.get("hostSession"):
            print("no host session bound", file=sys.stderr)
            return 2
        require_parseable_user_config()
        status = _send_wake(notification, load_config())
        if status == 202:
            return 0
        print(f"HTTP {status}", file=sys.stderr)
    except Exception:
        print("notification delivery failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
