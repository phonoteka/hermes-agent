from __future__ import annotations

from .attachments import _ATTACHMENT_SIZE_CAP_BYTES, _materialize_attachments
from .client import Bitrix24Client, _get_client
from .code import _handle_bitrix24_code_read, _handle_bitrix24_code_search
from .handoff import _agent_request_from_result
from .ingress import _IngressWebhookCheck, _OutboundWebhookCheck, _make_webhook_ingress_handler
from .schemas import (
    BITRIX24_CHAT_GET_SCHEMA,
    BITRIX24_CHAT_SEND_SCHEMA,
    BITRIX24_CODE_READ_SCHEMA,
    BITRIX24_CODE_SEARCH_SCHEMA,
    BITRIX24_MY_PLAN_STAGES_GET_SCHEMA,
    BITRIX24_MY_PLAN_TASK_MOVE_SCHEMA,
    BITRIX24_STATUS_SCHEMA,
    BITRIX24_TASK_COMMENT_ADD_SCHEMA,
    BITRIX24_TASK_COMMENT_UPDATE_SCHEMA,
    BITRIX24_TASK_COMMENTS_SCHEMA,
    BITRIX24_TASK_COMPLETE_SCHEMA,
    BITRIX24_TASK_CREATE_SCHEMA,
    BITRIX24_TASK_GET_SCHEMA,
    BITRIX24_TASK_LIST_SCHEMA,
    BITRIX24_TASK_UPDATE_SCHEMA,
    BITRIX24_WEBHOOK_INGRESS_SCHEMA,
)
from .tools import (
    _handle_bitrix24_chat_get,
    _handle_bitrix24_chat_send,
    _handle_bitrix24_my_plan_stages_get,
    _handle_bitrix24_my_plan_task_move,
    _handle_bitrix24_status,
    _handle_bitrix24_task_comment_add,
    _handle_bitrix24_task_comment_update,
    _handle_bitrix24_task_comments,
    _handle_bitrix24_task_complete,
    _handle_bitrix24_task_create,
    _handle_bitrix24_task_get,
    _handle_bitrix24_task_list,
    _handle_bitrix24_task_update,
)

_TOOL_SPECS = (
    {
        "name": "bitrix24_code_read",
        "schema": BITRIX24_CODE_READ_SCHEMA,
        "handler": _handle_bitrix24_code_read,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "📖",
    },
    {
        "name": "bitrix24_code_search",
        "schema": BITRIX24_CODE_SEARCH_SCHEMA,
        "handler": _handle_bitrix24_code_search,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "🔎",
    },
    {
        "name": "bitrix24_status",
        "schema": BITRIX24_STATUS_SCHEMA,
        "handler": _handle_bitrix24_status,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "📎",
    },
    {
        "name": "bitrix24_webhook_ingress",
        "schema": BITRIX24_WEBHOOK_INGRESS_SCHEMA,
        "handler": None,
        "requires_env": ["BITRIX24_WEBHOOK_URL", "BITRIX24_APPLICATION_TOKEN"],
        "emoji": "📨",
    },
    {
        "name": "bitrix24_task_create",
        "schema": BITRIX24_TASK_CREATE_SCHEMA,
        "handler": _handle_bitrix24_task_create,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "📝",
    },
    {
        "name": "bitrix24_task_get",
        "schema": BITRIX24_TASK_GET_SCHEMA,
        "handler": _handle_bitrix24_task_get,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "🔎",
    },
    {
        "name": "bitrix24_task_list",
        "schema": BITRIX24_TASK_LIST_SCHEMA,
        "handler": _handle_bitrix24_task_list,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "📋",
    },
    {
        "name": "bitrix24_task_update",
        "schema": BITRIX24_TASK_UPDATE_SCHEMA,
        "handler": _handle_bitrix24_task_update,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "✏️",
    },
    {
        "name": "bitrix24_task_complete",
        "schema": BITRIX24_TASK_COMPLETE_SCHEMA,
        "handler": _handle_bitrix24_task_complete,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "✅",
    },
    {
        "name": "bitrix24_task_comments",
        "schema": BITRIX24_TASK_COMMENTS_SCHEMA,
        "handler": _handle_bitrix24_task_comments,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "💬",
    },
    {
        "name": "bitrix24_task_comment_add",
        "schema": BITRIX24_TASK_COMMENT_ADD_SCHEMA,
        "handler": _handle_bitrix24_task_comment_add,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "➕",
    },
    {
        "name": "bitrix24_task_comment_update",
        "schema": BITRIX24_TASK_COMMENT_UPDATE_SCHEMA,
        "handler": _handle_bitrix24_task_comment_update,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "🛠️",
    },
    {
        "name": "bitrix24_chat_get",
        "schema": BITRIX24_CHAT_GET_SCHEMA,
        "handler": _handle_bitrix24_chat_get,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "🗨️",
    },
    {
        "name": "bitrix24_chat_send",
        "schema": BITRIX24_CHAT_SEND_SCHEMA,
        "handler": _handle_bitrix24_chat_send,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "📤",
    },
    {
        "name": "bitrix24_my_plan_stages_get",
        "schema": BITRIX24_MY_PLAN_STAGES_GET_SCHEMA,
        "handler": _handle_bitrix24_my_plan_stages_get,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "📊",
    },
    {
        "name": "bitrix24_my_plan_task_move",
        "schema": BITRIX24_MY_PLAN_TASK_MOVE_SCHEMA,
        "handler": _handle_bitrix24_my_plan_task_move,
        "requires_env": ["BITRIX24_WEBHOOK_URL"],
        "emoji": "🔧",
    },
)


def register(ctx) -> None:
    outbound_check_fn = _OutboundWebhookCheck()
    ingress_check_fn = _IngressWebhookCheck()
    ingress_handler = _make_webhook_ingress_handler(
        materialize_attachments=_materialize_attachments,
        agent_request_from_result=_agent_request_from_result,
    )

    for tool in _TOOL_SPECS:
        ctx.register_tool(
            name=tool["name"],
            toolset="bitrix24",
            schema=tool["schema"],
            handler=ingress_handler if tool["name"] == "bitrix24_webhook_ingress" else tool["handler"],
            check_fn=ingress_check_fn if tool["name"] == "bitrix24_webhook_ingress" else outbound_check_fn,
            requires_env=tool["requires_env"],
            emoji=tool["emoji"],
        )

    from .platform import PLATFORM_HINT, PLATFORM_NAME, Bitrix24Adapter

    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Bitrix24",
        adapter_factory=Bitrix24Adapter,
        check_fn=ingress_check_fn,
        validate_config=lambda config: ingress_check_fn(),
        required_env=["BITRIX24_WEBHOOK_URL", "BITRIX24_APPLICATION_TOKEN"],
        install_hint="Set BITRIX24_WEBHOOK_URL and BITRIX24_APPLICATION_TOKEN",
        allowed_users_env="BITRIX24_ALLOWED_USERS",
        allow_all_env="BITRIX24_ALLOW_ALL_USERS",
        platform_hint=PLATFORM_HINT,
        emoji="📎",
    )
