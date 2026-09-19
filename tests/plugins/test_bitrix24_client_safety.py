from __future__ import annotations

import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from plugins.bitrix24 import client as bitrix24_client


class _Response:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.parametrize(
    "raw_url",
    [
        "http://trusted.example/rest/1/secret-token",
        "https://127.0.0.1/rest/1/secret-token",
        "https://trusted.example/rest/1",
        "https://trusted.example/rest/1/secret-token/tasks.task.get",
        "https://trusted.example/rest/1/secret-token?auth=1",
    ],
)
def test_bitrix24_webhook_base_url_rejects_invalid_shapes(raw_url, monkeypatch):
    monkeypatch.setattr(bitrix24_client, "is_safe_url", lambda url: url != "https://127.0.0.1/rest/1/secret-token")

    with pytest.raises(RuntimeError, match="invalid_bitrix_webhook_url") as excinfo:
        bitrix24_client.Bitrix24Client(raw_url)

    assert "secret-token" not in str(excinfo.value)
    assert raw_url not in str(excinfo.value)


def test_bitrix24_webhook_base_url_normalizes_only_trailing_slash(monkeypatch):
    monkeypatch.setattr(bitrix24_client, "is_safe_url", lambda _: True)

    client = bitrix24_client.Bitrix24Client("https://trusted.example/rest/7/secret-token/")

    assert client.webhook_url == "https://trusted.example/rest/7/secret-token"


def test_bitrix24_client_call_builds_url_from_validated_base(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(bitrix24_client, "is_safe_url", lambda _: True)

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["method"] = request.get_method()
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _Response('{"result": true}')

    monkeypatch.setattr(bitrix24_client, "urlopen", fake_urlopen)

    result = bitrix24_client.Bitrix24Client("https://trusted.example/rest/1/secret-token").call(
        "tasks.task.get",
        taskId="42",
    )

    assert seen == {
        "url": "https://trusted.example/rest/1/secret-token/tasks.task.get",
        "timeout": 10,
        "method": "POST",
        "headers": {"Content-type": "application/json"},
        "body": {"taskId": "42"},
    }
    assert result == {"result": True}


def test_bitrix24_client_call_sanitizes_http_errors(monkeypatch):
    webhook_url = "https://trusted.example/rest/1/secret-token"
    monkeypatch.setattr(bitrix24_client, "is_safe_url", lambda _: True)

    def fake_urlopen(request, timeout=0):
        raise HTTPError(
            url=f"{webhook_url}/tasks.task.get",
            code=401,
            msg="Unauthorized",
            hdrs=Message(),
            fp=io.BytesIO(b'{"error":"secret-token should not leak"}'),
        )

    monkeypatch.setattr(bitrix24_client, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="bitrix_http_error:401") as excinfo:
        bitrix24_client.Bitrix24Client(webhook_url).call("tasks.task.get", taskId="42")

    assert webhook_url not in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)
    assert "Unauthorized" not in str(excinfo.value)


def test_bitrix24_client_call_sanitizes_invalid_json_response(monkeypatch):
    webhook_url = "https://trusted.example/rest/1/secret-token"
    monkeypatch.setattr(bitrix24_client, "is_safe_url", lambda _: True)
    monkeypatch.setattr(
        bitrix24_client,
        "urlopen",
        lambda request, timeout=0: _Response(f"not json from {webhook_url}/tasks.task.get"),
    )

    with pytest.raises(RuntimeError, match="invalid_json_response") as excinfo:
        bitrix24_client.Bitrix24Client(webhook_url).call("tasks.task.get", taskId="42")

    assert webhook_url not in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)


def test_bitrix24_client_call_sanitizes_transport_errors(monkeypatch):
    webhook_url = "https://trusted.example/rest/1/secret-token"
    monkeypatch.setattr(bitrix24_client, "is_safe_url", lambda _: True)
    monkeypatch.setattr(
        bitrix24_client,
        "urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(URLError(f"timeout contacting {webhook_url}/tasks.task.get")),
    )

    with pytest.raises(RuntimeError, match=r"transport_error:timeout contacting \[redacted_url\]") as excinfo:
        bitrix24_client.Bitrix24Client(webhook_url).call("tasks.task.get", taskId="42")

    assert webhook_url not in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)
