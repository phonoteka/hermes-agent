from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_source(
    *,
    platform: Platform = Platform.DISCORD,
    user_id: str = "user1",
    chat_type: str = "dm",
    chat_id: str = "c1",
) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name=f"name-{user_id}",
        chat_type=chat_type,
    )


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_runner(*, platform_extra: dict | None = None, platform: Platform = Platform.DISCORD):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            platform: PlatformConfig(
                enabled=True,
                token="***",
                extra=platform_extra or {},
            )
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    session_entry = SessionEntry(
        session_key="agent:main:discord:dm:c1",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_hook_rewrite_rechecked_after_canonicalization() -> None:
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": [],
        }
    )

    runner.hooks.emit_collect = AsyncMock(
        return_value=[
            {
                "decision": "rewrite",
                "command_name": "restart",
                "raw_args": "",
            }
        ]
    )
    runner._handle_restart_command = AsyncMock(return_value="restart-handled")

    result = await runner._handle_message(
        _make_event("/help", _make_source(user_id="999"))
    )

    assert result is not None
    assert "⛔" in result
    assert "/restart is admin-only here" in result


@pytest.mark.asyncio
async def test_unknown_quick_exec_cannot_bypass_slash_gate() -> None:
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": [],
        }
    )
    runner.config.quick_commands = {
        "danger": {
            "type": "exec",
            "command": "printf BYPASS",
        }
    }

    result = await runner._handle_message(
        _make_event("/danger", _make_source(user_id="999"))
    )

    assert result is not None
    assert "⛔" in result
    assert "/danger is admin-only here" in result
