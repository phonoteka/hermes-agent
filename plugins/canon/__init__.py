"""Bundled ordinary Canon CLI adapter for Hermes."""

from __future__ import annotations

from typing import Any

from .schemas import (
    CANON_GATEWAY_CHECKPOINT_SCHEMA,
    CANON_GATEWAY_LAUNCH_SCHEMA,
    CANON_WORKFLOW_CONTROL_SCHEMA,
    CANON_WORKFLOW_OBSERVE_SCHEMA,
)
from .transport import (
    handle_checkpoint,
    handle_control,
    handle_observe,
    handle_tool,
)

_TOOLS = (
    ("canon_gateway_launch", CANON_GATEWAY_LAUNCH_SCHEMA, handle_tool, "🚦"),
    ("canon_gateway_checkpoint", CANON_GATEWAY_CHECKPOINT_SCHEMA, handle_checkpoint, "⏮️"),
    ("canon_workflow_observe", CANON_WORKFLOW_OBSERVE_SCHEMA, handle_observe, "🔭"),
    ("canon_workflow_control", CANON_WORKFLOW_CONTROL_SCHEMA, handle_control, "⏯️"),
)


def register(ctx: Any) -> None:
    """Register exactly the four ordinary Canon tools through PluginContext."""
    async def _launch_handler(args: dict[str, Any], **kwargs: Any) -> str:
        return await handle_tool(args, ctx=ctx, **kwargs)

    ctx.register_tool(
        name="canon_gateway_launch",
        toolset="canon",
        schema=CANON_GATEWAY_LAUNCH_SCHEMA,
        handler=_launch_handler,
        is_async=True,
        emoji="🚦",
        description=CANON_GATEWAY_LAUNCH_SCHEMA["description"],
    )
    for name, schema, handler, emoji in _TOOLS[1:]:
        async def _handler(args: dict[str, Any], _handler=handler, **kwargs: Any) -> str:
            return await _handler(args, ctx=ctx, **kwargs)

        ctx.register_tool(
            name=name,
            toolset="canon",
            schema=schema,
            handler=_handler,
            is_async=True,
            emoji=emoji,
            description=schema["description"],
        )


__all__ = ["register"]
