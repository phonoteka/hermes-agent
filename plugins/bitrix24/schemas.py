from __future__ import annotations

_POSITIVE_ID_SCHEMA = {
    "anyOf": [
        {"type": "integer", "minimum": 1},
        {"type": "string", "pattern": "^[1-9][0-9]*$"},
    ]
}

BITRIX24_CODE_READ_SCHEMA = {
    "name": "bitrix24_code_read",
    "description": "Read numbered lines from one tracked source file in a configured Plastinka code root (backend or editor). Only available inside a Bitrix24 task session; read-only, not arbitrary filesystem access.",
    "parameters": {
        "type": "object",
        "properties": {
            "root": {"type": "string", "enum": ["backend", "editor"]},
            "path": {"type": "string", "minLength": 1},
            "start_line": {"type": "integer", "minimum": 1},
            "line_count": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["root", "path"],
        "additionalProperties": False,
    },
}

BITRIX24_CODE_SEARCH_SCHEMA = {
    "name": "bitrix24_code_search",
    "description": "Literal case-insensitive search of tracked source in the configured Plastinka backend or editor code root. Only available inside a Bitrix24 task session; returns bounded matching lines, read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "root": {"type": "string", "enum": ["backend", "editor"]},
            "query": {"type": "string", "minLength": 2, "maxLength": 120},
        },
        "required": ["root", "query"],
        "additionalProperties": False,
    },
}

BITRIX24_STATUS_SCHEMA = {
    "name": "bitrix24_status",
    "description": (
        "Return scaffold-only Bitrix24 plugin status as JSON. Supports only old REST webhook bases "
        "in the form https://{portal}/rest/{user}/{secret}; REST v3 /rest/api/{user}/{token} is unsupported. "
        "No live portal mutation."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

BITRIX24_WEBHOOK_INGRESS_SCHEMA = {
    "name": "bitrix24_webhook_ingress",
    "description": (
        "Validate, authenticate, normalize, and enrich a Bitrix24 outgoing webhook envelope "
        "before downstream processing. Outbound Hermes -> Bitrix24 still uses the configured old REST "
        "webhook base https://{portal}/rest/{user}/{secret}; REST v3 /rest/api/{user}/{token} is unsupported. "
        "Inbound Bitrix24 -> Hermes is for the dedicated POST /bitrix24/webhook gateway route when "
        "platforms.webhook.extra.bitrix24.enabled=true, authenticates with payload auth.application_token + "
        "auth.domain instead of Hermes generic HMAC headers, accepts JSON and application/x-www-form-urlencoded "
        "payloads, and can derive retry-stable event_id values from event/event_handler_id/material. Public outputs "
        "redact BITRIX24_WEBHOOK_URL, BITRIX24_APPLICATION_TOKEN, auth.application_token, DOWNLOAD_URL, and raw "
        "download links while returning safe attachment metadata and mocked handoff/deferred tool candidates without "
        "mutating live tasks inline. Do not send raw Bitrix24 outgoing webhooks to generic /webhooks/<name> unless "
        "an external relay adds Hermes HMAC/Svix/GitLab-style signing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event": {"type": "string"},
            "event_id": {"type": "string"},
            "auth": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "application_token": {"type": "string"},
                },
                "required": ["application_token"],
                "additionalProperties": True,
            },
            "data": {"type": "object"},
        },
        "required": ["event", "event_id", "auth", "data"],
        "additionalProperties": True,
    },
}

BITRIX24_TASK_CREATE_SCHEMA = {
    "name": "bitrix24_task_create",
    "description": (
        "Create a Bitrix24 task via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. REST v3 /rest/api/{user}/{token} is unsupported; "
        "public outputs do not echo webhook URLs or application tokens."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "responsible_id": _POSITIVE_ID_SCHEMA,
            "creator_id": _POSITIVE_ID_SCHEMA,
        },
        "required": ["title", "responsible_id"],
        "additionalProperties": False,
    },
}

BITRIX24_TASK_GET_SCHEMA = {
    "name": "bitrix24_task_get",
    "description": (
        "Read a Bitrix24 task via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}

BITRIX24_TASK_LIST_SCHEMA = {
    "name": "bitrix24_task_list",
    "description": (
        "List Bitrix24 tasks via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. Supports bounded order/filter/select inputs only; "
        "REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order": {"type": "object"},
            "filter": {"type": "object"},
            "select": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "additionalProperties": False,
    },
}

BITRIX24_TASK_UPDATE_SCHEMA = {
    "name": "bitrix24_task_update",
    "description": (
        "Update Bitrix24 task fields via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
            "fields": {"type": "object"},
        },
        "required": ["task_id", "fields"],
        "additionalProperties": False,
    },
}

BITRIX24_TASK_COMPLETE_SCHEMA = {
    "name": "bitrix24_task_complete",
    "description": (
        "Complete a Bitrix24 task via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}

BITRIX24_TASK_COMMENTS_SCHEMA = {
    "name": "bitrix24_task_comments",
    "description": (
        "Read Bitrix24 task comments from the current task chat via im.dialog.messages.get "
        "after resolving task chatId and normalizing numeric chat ids to DIALOG_ID=chat<chatId>. "
        "Uses the old REST webhook base https://{portal}/rest/{user}/{secret} and returns a unified "
        "JSON result envelope. REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}

BITRIX24_TASK_COMMENT_ADD_SCHEMA = {
    "name": "bitrix24_task_comment_add",
    "description": (
        "Add a Bitrix24 task comment via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. Accepts only task_id plus a non-empty message; "
        "REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
            "message": {"type": "string"},
        },
        "required": ["task_id", "message"],
        "additionalProperties": False,
    },
}

BITRIX24_TASK_COMMENT_UPDATE_SCHEMA = {
    "name": "bitrix24_task_comment_update",
    "description": (
        "Update a Bitrix24 task comment via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. Accepts only task_id, comment_id, and a non-empty message; "
        "REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
            "comment_id": _POSITIVE_ID_SCHEMA,
            "message": {"type": "string"},
        },
        "required": ["task_id", "comment_id", "message"],
        "additionalProperties": False,
    },
}

BITRIX24_CHAT_GET_SCHEMA = {
    "name": "bitrix24_chat_get",
    "description": (
        "Read a Bitrix24 chat transcript via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. Numeric chat ids are normalized to IM dialog ids "
        "as chat<id>; already-prefixed dialog ids are preserved. REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": ["string", "integer"]},
        },
        "required": ["chat_id"],
        "additionalProperties": False,
    },
}

BITRIX24_MY_PLAN_STAGES_GET_SCHEMA = {
    "name": "bitrix24_my_plan_stages_get",
    "description": (
        "Read the current webhook user's Bitrix24 My Plan stages via task.stages.get with entityId=0. "
        "Uses the old REST webhook base https://{portal}/rest/{user}/{secret}; "
        "REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

BITRIX24_MY_PLAN_TASK_MOVE_SCHEMA = {
    "name": "bitrix24_my_plan_task_move",
    "description": (
        "Move a task to a different stage via task.stages.movetask after "
        "task.stages.get confirms that stage_id belongs to the target scope. "
        "Use entity_id=0 (default) for the current user's My Plan (ENTITY_TYPE=U); "
        "pass a group_id as entity_id for a Kanban board (ENTITY_TYPE=G). "
        "Supports at most one before or after anchor. "
        "Uses the old REST webhook base https://{portal}/rest/{user}/{secret}; "
        "REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": _POSITIVE_ID_SCHEMA,
            "stage_id": _POSITIVE_ID_SCHEMA,
            "entity_id": _POSITIVE_ID_SCHEMA,
            "before_task_id": _POSITIVE_ID_SCHEMA,
            "after_task_id": _POSITIVE_ID_SCHEMA,
        },
        "required": ["task_id", "stage_id"],
        "additionalProperties": False,
    },
}

BITRIX24_CHAT_SEND_SCHEMA = {
    "name": "bitrix24_chat_send",
    "description": (
        "Send a Bitrix24 chat message via the old REST webhook base https://{portal}/rest/{user}/{secret} "
        "and return a unified JSON result envelope. Accepts only chat_id plus a non-empty message; "
        "REST v3 /rest/api/{user}/{token} is unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": ["string", "integer"]},
            "message": {"type": "string"},
        },
        "required": ["chat_id", "message"],
        "additionalProperties": False,
    },
}
