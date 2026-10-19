"""Register mechanical Canon tools and the gateway-wide pause wake listener."""
from __future__ import annotations

from typing import Any

from .wake_listener import WakeListener
from .schemas import TOOL_SCHEMAS
from .transport import handle_command


def register(ctx: Any) -> None:
    """Pre: ctx supports tool registration, injection and unload callbacks.
    Post: registers eleven CLI tools without aliases and starts one wake listener
    whose stop callback is owned by plugin unload. Raises: registration or
    worker startup errors unchanged.
    """
    for command, schema in TOOL_SCHEMAS.items():
        async def handler(args: dict[str, Any], _command: str = command, **kwargs: Any) -> str:
            """Pre: CLI-schema args. Post: delegate transport. Raises: none."""
            return await handle_command(_command, args, ctx=ctx, **kwargs)

        ctx.register_tool(name=schema["name"], toolset="canon", schema=schema,
                          handler=handler, is_async=True, emoji="🚦", description=schema["description"])
    listener = WakeListener(ctx)
    listener.start()
    ctx.on_unload(listener.stop)


__all__ = ["register"]
