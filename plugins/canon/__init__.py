"""Register one mechanical Hermes tool per Canon CLI command."""
from __future__ import annotations

from typing import Any

from .schemas import TOOL_SCHEMAS
from .transport import handle_command


def register(ctx: Any) -> None:
    """Pre: ctx supports registration. Post: eleven CLI tools, no aliases. Raises: registration errors unchanged."""
    for command, schema in TOOL_SCHEMAS.items():
        async def handler(args: dict[str, Any], _command: str = command, **kwargs: Any) -> str:
            """Pre: CLI-schema args. Post: delegate transport. Raises: none."""
            return await handle_command(_command, args, ctx=ctx, **kwargs)

        ctx.register_tool(name=schema["name"], toolset="canon", schema=schema,
                          handler=handler, is_async=True, emoji="🚦", description=schema["description"])


__all__ = ["register"]
