from __future__ import annotations

import json
from typing import Any

import pytest

@pytest.fixture(autouse=True)
def fake_event_source(monkeypatch):
    from plugins.bitrix24 import ingress
    from tests.plugins.test_bitrix24_event_enrichment import FakeEventSource

    source = FakeEventSource('@hermes проверь вложения')
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    return source

import yaml

_APP_TOKEN = "expected-app-token"


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_platform(self, **kwargs) -> None:
        self.platforms = [*getattr(self, "platforms", []), kwargs]


class _MockBitrix24Client:
    def __init__(self, metadata_by_id: dict[str, dict[str, Any]]) -> None:
        self.metadata_by_id = metadata_by_id
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((method, payload))
        assert method == "disk.file.get"
        file_id = str(payload.get("id"))
        return {"result": self.metadata_by_id[file_id]}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@hermes")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    return hermes_home


@pytest.fixture
def ingress_runtime(hermes_home):
    import plugins.bitrix24 as bitrix24
    from plugins.bitrix24 import attachments

    ctx = _CapturingContext()
    bitrix24.register(ctx)
    tool = next((tool for tool in ctx.tools if tool["name"] == "bitrix24_webhook_ingress"), None)
    if tool is None:
        pytest.fail(
            "AC-5 REQ-4: public ingress seam missing; expected plugins.bitrix24.register(ctx) "
            "to expose bitrix24_webhook_ingress before files/vision slice tests run"
        )
    return bitrix24, attachments, tool


@pytest.fixture
def attachment_metadata() -> dict[str, dict[str, Any]]:
    return {
        "111": {
            "ID": "111",
            "DOWNLOAD_URL": "https://example.com/attachments/111.png",
            "CONTENT_TYPE": "image/png",
            "SIZE": 2048,
        },
        "222": {
            "ID": "222",
            "DOWNLOAD_URL": "https://example.com/attachments/222.pdf",
            "CONTENT_TYPE": "application/pdf",
            "SIZE": 4096,
        },
        "333": {
            "ID": "333",
            "DOWNLOAD_URL": "http://example.invalid/attachments/333.png",
            "CONTENT_TYPE": "image/png",
            "SIZE": 1024,
        },
        "444": {
            "ID": "444",
            "DOWNLOAD_URL": "https://example.com/attachments/444.png",
            "CONTENT_TYPE": "image/png",
            "SIZE": 10 * 1024 * 1024 + 1,
        },
    }


def _comment_payload(*, event_id: str = "evt-files-1") -> dict:
    return {
        "event_id": event_id,
        "event": "ONTASKCOMMENTADD",
        "auth": {"domain": "example.com", "application_token": _APP_TOKEN},
        "data": {
            "FIELDS_AFTER": {
                "ID": "0",
                "MESSAGE_ID": "84",
                "TASK_ID": "42",
                "FILE_IDS": ["111", "222", "333", "444"],
            }
        },
    }


def _invoke(tool: dict, payload: dict, *, task_id: str = "build-fix-files") -> dict:
    return json.loads(tool["handler"](payload, task_id=task_id))


def test_bitrix24_files_comment_event_materializes_attachment_surface_before_vision(
    ingress_runtime,
    attachment_metadata,
    monkeypatch,
):
    bitrix24, attachments, ingress_tool = ingress_runtime
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", _APP_TOKEN)
    mock_client = _MockBitrix24Client(attachment_metadata)
    monkeypatch.setattr(attachments, "_get_client", lambda: mock_client)

    result = _invoke(ingress_tool, _comment_payload())

    assert result["accepted"] is True
    assert result["reason"] == "targeted_event"
    assert result["targeting"]["mode"] == "bot_mention"
    assert result["task_id"] == "42"
    assert result["message"] == "@hermes проверь вложения"
    assert result["file_ids"] == ["111", "222", "333", "444"]
    assert result["turns"] == [
        {
            "source": "bitrix24",
            "event": "ONTASKCOMMENTADD",
            "event_id": "evt-files-1",
            "object_id": "84",
        }
    ]
    assert [item["file_id"] for item in result["attachments"]] == ["111", "222", "333", "444"]
    assert all("download_url" not in item for item in result["attachments"])
    assert mock_client.calls == [
        ("disk.file.get", {"id": "111"}),
        ("disk.file.get", {"id": "222"}),
        ("disk.file.get", {"id": "333"}),
        ("disk.file.get", {"id": "444"}),
    ]


def test_bitrix24_files_routes_images_to_vision_and_non_images_to_safe_records(
    ingress_runtime,
    attachment_metadata,
    monkeypatch,
):
    bitrix24, attachments, ingress_tool = ingress_runtime
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", _APP_TOKEN)
    monkeypatch.setattr(attachments, "_get_client", lambda: _MockBitrix24Client(attachment_metadata))

    result = _invoke(ingress_tool, _comment_payload(event_id="evt-files-2"))
    attachments_by_id = {item["file_id"]: item for item in result["attachments"]}

    image = attachments_by_id["111"]
    assert image["kind"] == "image"
    assert "download_url" not in image
    assert image["vision"]["requested"] is True
    assert image["vision"]["tool"] == "vision_analyze"
    assert image["vision"]["task_context"] == {
        "source": "bitrix24",
        "event": "ONTASKCOMMENTADD",
        "event_id": "evt-files-2",
        "source_domain": "example.com",
        "task_id": "42",
        "message": "@hermes проверь вложения",
    }

    non_image = attachments_by_id["222"]
    assert non_image["kind"] == "file"
    assert non_image["reason"] == "non_image"
    assert "vision" not in non_image

    blocked_url = attachments_by_id["333"]
    assert blocked_url["kind"] == "blocked"
    assert blocked_url["reason"] == "blocked_url"

    oversize = attachments_by_id["444"]
    assert oversize["kind"] == "blocked"
    assert oversize["reason"] == "size_cap"
    assert oversize["size_cap_bytes"] == bitrix24._ATTACHMENT_SIZE_CAP_BYTES


def test_bitrix24_files_marks_attachment_metadata_unavailable_when_lookup_fails(ingress_runtime, monkeypatch):
    _, attachments, ingress_tool = ingress_runtime
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", _APP_TOKEN)

    class _FailingBitrix24Client:
        def call(self, method: str, **payload: Any) -> dict[str, Any]:
            assert method == "disk.file.get"
            assert payload == {"id": "777"}
            raise RuntimeError("metadata api unavailable")

    monkeypatch.setattr(attachments, "_get_client", lambda: _FailingBitrix24Client())

    result = _invoke(
        ingress_tool,
        {
            "event_id": "evt-files-error-1",
            "event": "ONTASKCOMMENTADD",
            "auth": {"domain": "example.com", "application_token": _APP_TOKEN},
            "data": {
                "FIELDS_AFTER": {
                    "ID": "0",
                "MESSAGE_ID": "84",
                    "TASK_ID": "42",
                    "FILE_IDS": ["777"],
                }
            },
        },
    )

    assert result["attachments"] == [
        {
            "file_id": "777",
            "kind": "blocked",
            "reason": "metadata_unavailable",
        }
    ]
    assert "metadata api unavailable" not in json.dumps(result)


@pytest.mark.parametrize(
    ("raw_size", "expected_size_bytes"),
    [("2048", 2048), (" 2048 ", 2048), (True, None)],
)
def test_bitrix24_files_coerces_attachment_size_edges(raw_size, expected_size_bytes):
    from plugins.bitrix24.attachments import _coerce_attachment_size

    assert _coerce_attachment_size(raw_size) == expected_size_bytes
