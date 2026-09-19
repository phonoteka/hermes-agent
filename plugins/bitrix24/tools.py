from __future__ import annotations

import json
import os
from typing import Any

from . import client as bitrix24_client
from .client import _call_method, _json_envelope, _positive_string_id, _string_id, _validated_webhook_base_url


def _handle_bitrix24_status(args: dict[str, Any] | None = None, **_: Any) -> str:
    try:
        _validated_webhook_base_url(os.getenv("BITRIX24_WEBHOOK_URL", ""))
        webhook_configured = True
    except RuntimeError:
        webhook_configured = False
    return json.dumps(
        {
            "ok": True,
            "plugin": "bitrix24",
            "status": "scaffold",
            "webhook_configured": webhook_configured,
        }
    )


def _handle_bitrix24_my_plan_stages_get(args: dict[str, Any] | None = None, **_: Any) -> str:
    return _call_method("task.stages.get", entityId=0)


def _handle_bitrix24_my_plan_task_move(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
        stage_id = _positive_string_id(payload.get("stage_id"), field="stage_id")
        entity_id = (
            _positive_string_id(payload.get("entity_id"), field="entity_id")
            if "entity_id" in payload
            else "0"
        )
        before_task_id = (
            _positive_string_id(payload.get("before_task_id"), field="before_task_id")
            if "before_task_id" in payload
            else None
        )
        after_task_id = (
            _positive_string_id(payload.get("after_task_id"), field="after_task_id")
            if "after_task_id" in payload
            else None
        )
    except ValueError as exc:
        return _json_envelope(method="task.stages.movetask", error=str(exc))

    if before_task_id is not None and after_task_id is not None:
        return _json_envelope(
            method="task.stages.movetask",
            error="before_task_id and after_task_id cannot both be provided",
        )

    expected_entity_type = "U" if entity_id == "0" else "G"

    preflight_raw = _call_method("task.stages.get", entityId=entity_id)
    try:
        preflight_envelope = json.loads(preflight_raw)
    except (TypeError, json.JSONDecodeError):
        return _json_envelope(method="task.stages.movetask", error="malformed stage preflight envelope")

    if not isinstance(preflight_envelope, dict):
        return _json_envelope(method="task.stages.movetask", error="malformed stage preflight envelope")
    if preflight_envelope.get("success") is False:
        return preflight_raw

    raw_result = preflight_envelope.get("result")
    stages = raw_result.get("result") if isinstance(raw_result, dict) else None
    if isinstance(stages, dict):
        stages = list(stages.values())
    if not isinstance(stages, list):
        return _json_envelope(method="task.stages.movetask", error="malformed stage preflight result")

    matching_stage = any(
        isinstance(stage, dict)
        and str(stage.get("ID")) == stage_id
        and stage.get("ENTITY_TYPE") == expected_entity_type
        for stage in stages
    )
    if not matching_stage:
        scope = "My Plan" if expected_entity_type == "U" else f"Kanban group {entity_id}"
        return _json_envelope(
            method="task.stages.movetask",
            error=f"stage_id is unknown or not a {scope} stage",
        )

    move_payload: dict[str, Any] = {"id": task_id, "stageId": stage_id}
    if before_task_id is not None:
        move_payload["before"] = before_task_id
    elif after_task_id is not None:
        move_payload["after"] = after_task_id
    return _call_method(
        "task.stages.movetask",
        id_field="task_id",
        identifier=task_id,
        **move_payload,
    )


def _handle_bitrix24_task_create(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return _json_envelope(method="tasks.task.add", error="title is required")
    try:
        responsible_id = _positive_string_id(payload.get("responsible_id"), field="responsible_id")
    except ValueError as exc:
        return _json_envelope(method="tasks.task.add", error=str(exc))
    description = payload.get("description")
    fields = {
        "TITLE": title.strip(),
        "RESPONSIBLE_ID": responsible_id,
    }
    if isinstance(description, str) and description.strip():
        fields["DESCRIPTION"] = description.strip()
    creator_id = payload.get("creator_id")
    if creator_id is not None:
        try:
            fields["CREATED_BY"] = _positive_string_id(creator_id, field="creator_id")
        except ValueError as exc:
            return _json_envelope(method="tasks.task.add", error=str(exc))
    return _call_method("tasks.task.add", id_field="task_id", fields=fields)


def _handle_bitrix24_task_get(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
    except ValueError as exc:
        return _json_envelope(method="tasks.task.get", error=str(exc))
    return _call_method("tasks.task.get", id_field="task_id", identifier=task_id, taskId=task_id)


def _handle_bitrix24_task_list(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    request_payload: dict[str, Any] = {}

    order = payload.get("order")
    if order is not None:
        if not isinstance(order, dict):
            return _json_envelope(method="tasks.task.list", error="order must be an object")
        request_payload["order"] = order

    filter_payload = payload.get("filter")
    if filter_payload is not None:
        if not isinstance(filter_payload, dict):
            return _json_envelope(method="tasks.task.list", error="filter must be an object")
        request_payload["filter"] = filter_payload

    select = payload.get("select")
    if select is not None:
        if not isinstance(select, list) or any(not isinstance(field, str) for field in select):
            return _json_envelope(method="tasks.task.list", error="select must be an array of strings")
        request_payload["select"] = select

    return _call_method("tasks.task.list", **request_payload)


def _handle_bitrix24_task_update(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
    except ValueError as exc:
        return _json_envelope(method="tasks.task.update", error=str(exc))
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return _json_envelope(
            method="tasks.task.update",
            id_field="task_id",
            identifier=task_id,
            error="fields is required",
        )
    return _call_method(
        "tasks.task.update",
        id_field="task_id",
        identifier=task_id,
        taskId=task_id,
        fields=fields,
    )


def _handle_bitrix24_task_complete(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
    except ValueError as exc:
        return _json_envelope(method="tasks.task.complete", error=str(exc))
    return _call_method(
        "tasks.task.complete",
        id_field="task_id",
        identifier=task_id,
        taskId=task_id,
    )


def _chat_dialog_id(value: Any) -> str | None:
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    chat_id = value.strip()
    if not chat_id:
        return None
    if chat_id.isdigit():
        return f"chat{chat_id}"
    return chat_id


def _first_chat_id(value: Any, *, allow_generic_id: bool = True) -> str | None:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("chatId", "CHAT_ID", "chat_id"):
            found = _first_chat_id(value.get(key), allow_generic_id=allow_generic_id)
            if found is not None:
                return found
        if allow_generic_id:
            for key in ("ID", "id"):
                found = _first_chat_id(value.get(key), allow_generic_id=allow_generic_id)
                if found is not None:
                    return found
        for key in ("task", "chat", "result"):
            found = _first_chat_id(value.get(key), allow_generic_id=allow_generic_id)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_chat_id(item, allow_generic_id=allow_generic_id)
            if found is not None:
                return found
    return None


def _handle_bitrix24_task_comments(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
    except ValueError as exc:
        return _json_envelope(method="im.dialog.messages.get", error=str(exc))

    try:
        client = bitrix24_client._get_client()
        task_result = client.call("tasks.task.get", taskId=task_id, select=["ID", "CHAT_ID"])
        chat_id = _first_chat_id(task_result, allow_generic_id=False)
        if chat_id is None:
            chat_result = client.call("im.chat.get", ENTITY_TYPE="TASKS_TASK", ENTITY_ID=task_id)
            chat_id = _first_chat_id(chat_result)
        dialog_id = _chat_dialog_id(chat_id)
        if dialog_id is None:
            return _json_envelope(
                method="im.dialog.messages.get",
                id_field="task_id",
                identifier=task_id,
                error="task chat_id is missing",
            )
        result = client.call("im.dialog.messages.get", DIALOG_ID=dialog_id)
    except Exception as exc:
        return _json_envelope(
            method="im.dialog.messages.get",
            id_field="task_id",
            identifier=task_id,
            error=str(exc),
        )
    return _json_envelope(
        method="im.dialog.messages.get",
        id_field="task_id",
        identifier=task_id,
        result=result,
    )


def _comment_message(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("message is required")
    return value.strip()


def _handle_bitrix24_task_comment_add(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
        message = _comment_message(payload.get("message"))
    except ValueError as exc:
        return _json_envelope(method="task.commentitem.add", error=str(exc))
    return _call_method(
        "task.commentitem.add",
        id_field="task_id",
        identifier=task_id,
        taskId=task_id,
        fields={"POST_MESSAGE": message},
    )


def _handle_bitrix24_task_comment_update(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        task_id = _positive_string_id(payload.get("task_id"), field="task_id")
        comment_id = _positive_string_id(payload.get("comment_id"), field="comment_id")
        message = _comment_message(payload.get("message"))
    except ValueError as exc:
        return _json_envelope(method="task.commentitem.update", error=str(exc))
    return _call_method(
        "task.commentitem.update",
        id_field="task_id",
        identifier=task_id,
        taskId=task_id,
        itemId=comment_id,
        fields={"POST_MESSAGE": message},
    )


def _handle_bitrix24_chat_get(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        chat_id = _string_id(payload.get("chat_id"), field="chat_id")
    except ValueError as exc:
        return _json_envelope(method="im.dialog.messages.get", error=str(exc))
    return _call_method(
        "im.dialog.messages.get",
        id_field="chat_id",
        identifier=chat_id,
        DIALOG_ID=_chat_dialog_id(chat_id),
    )


def _handle_bitrix24_chat_send(args: dict[str, Any] | None = None, **_: Any) -> str:
    payload = args if isinstance(args, dict) else {}
    try:
        chat_id = _string_id(payload.get("chat_id"), field="chat_id")
        message = _comment_message(payload.get("message"))
    except ValueError as exc:
        return _json_envelope(method="im.message.add", error=str(exc))
    return _call_method(
        "im.message.add",
        id_field="chat_id",
        identifier=chat_id,
        DIALOG_ID=chat_id,
        MESSAGE=message,
    )
