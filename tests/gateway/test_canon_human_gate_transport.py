"""Transport-level RED guards for Canon human-gate Telegram delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter
from gateway.platforms.base import SendResult


class _AuthRunner:
    def _is_user_authorized(self, source):
        return True

    async def _handle_message(self, event):
        return None


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._bot.username = "test_bot"
    adapter._app = MagicMock()
    runner = _AuthRunner()
    adapter._message_handler = runner._handle_message
    return adapter


def _declared_callbacks() -> list[dict[str, str]]:
    return [
        {"label": "✅ Approve", "callbackData": '{"gateId":"gate-r05","action":"approve"}'},
        {"label": "✏️ Revise", "callbackData": '{"gateId":"gate-r05","action":"revise"}'},
        {"label": "❌ Reject", "callbackData": '{"gateId":"gate-r05","action":"reject"}'},
    ]


@pytest.mark.asyncio
async def test_telegram_transport_does_not_hardcode_current_gateway_actions() -> None:
    """AC-R05-001 RED: Telegram transport must render workflow-declared review actions.

    pre: Canon current-gateway sender provides explicit callback descriptors for one
         human-gate review card.
    post: Telegram buttons preserve the declared labels/callback payloads instead of
          hardcoding cg:y/n/e current-gateway semantics.
    raises: AssertionError while transport still rewrites review actions to legacy
            yes/no/corrections buttons.
    """

    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="43"))
    buttons = []

    declared_callbacks = _declared_callbacks()

    def fake_button(text, callback_data=None, **kwargs):
        button = SimpleNamespace(text=text, callback_data=callback_data, kwargs=kwargs)
        buttons.append(button)
        return button

    with patch("gateway.platforms.telegram.InlineKeyboardButton", side_effect=fake_button):
        result = await adapter.send_canon_review_prompt(
            chat_id="12345",
            message="Canon review card",
            run_id="legacy-run-id",
            metadata={
                "thread_id": "999",
                "callbacks": declared_callbacks,
                "gate_identity": {"id": "gate-r05", "runId": "canon-r05-run"},
                "downloadableArtifacts": [
                    {
                        "path": "/tmp/canon-review-package.json",
                        "fileName": "canon-review-package.json",
                        "caption": "Canon full review package",
                    }
                ],
            },
        )

    assert result.success is True
    assert [button.text for button in buttons] == [callback["label"] for callback in declared_callbacks]
    assert [button.callback_data for button in buttons] == [callback["callbackData"] for callback in declared_callbacks]
    kwargs = adapter._bot.send_message.call_args[1]
    assert kwargs.get("message_thread_id") == 999
    assert kwargs.get("parse_mode") is None
    adapter.send_document.assert_awaited_once_with(
        chat_id="12345",
        file_path="/tmp/canon-review-package.json",
        caption="Canon full review package",
        file_name="canon-review-package.json",
        reply_to="42",
        metadata={
            "thread_id": "999",
            "callbacks": _declared_callbacks(),
            "gate_identity": {"id": "gate-r05", "runId": "canon-r05-run"},
            "downloadableArtifacts": [
                {
                    "path": "/tmp/canon-review-package.json",
                    "fileName": "canon-review-package.json",
                    "caption": "Canon full review package",
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_telegram_transport_rejects_declared_prompt_without_downloadable_artifact() -> None:
    """Current-gateway review delivery must include a downloadable full artifact package."""

    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))

    result = await adapter.send_canon_review_prompt(
        chat_id="12345",
        message="Canon review card",
        run_id="legacy-run-id",
        metadata={
            "thread_id": "999",
            "callbacks": _declared_callbacks(),
            "gate_identity": {"id": "gate-r05", "runId": "canon-r05-run"},
        },
    )

    assert result.success is False
    assert "downloadable" in str(result.error).lower()
    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_transport_rejects_declared_current_gateway_prompt_without_valid_callbacks() -> None:
    """AC-R05-001: current-gateway declared review prompts must fail closed on malformed callbacks.

    pre: Canon current-gateway metadata marks the prompt as gate-bound review delivery but
         omits a usable declared callbacks array.
    post: Telegram transport returns a send failure and never falls back to legacy
          `cg:y/n/e:<run>` buttons for the declared-action path.
    raises: AssertionError while malformed declared metadata still sends a legacy review card.
    """

    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))

    result = await adapter.send_canon_review_prompt(
        chat_id="12345",
        message="Canon review card",
        run_id="legacy-run-id",
        metadata={
            "thread_id": "999",
            "callbacks": [{"label": "✅ Approve"}],
            "gate_identity": {"id": "gate-r05", "runId": "canon-r05-run"},
        },
    )

    assert result.success is False
    adapter._bot.send_message.assert_not_called()
