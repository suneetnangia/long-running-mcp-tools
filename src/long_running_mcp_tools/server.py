"""Task-enabled FastMCP server with a configurable delayed greeting."""

import asyncio
from typing import Annotated

from fastmcp import FastMCP
from fastmcp_tasks import TasksExtension
from pydantic import Field

mcp = FastMCP("Long-Running MCP Tools")
mcp.add_extension(TasksExtension())


@mcp.tool(task=True)
async def delayed_hello(
    delay_ms: Annotated[
        int,
        Field(
            ge=10,
            le=600_000,
            description="Delay in milliseconds, from 10 ms through 10 minutes.",
        ),
    ] = 1_000,
) -> str:
    """Return a greeting after the requested delay."""
    await asyncio.sleep(delay_ms / 1_000)
    return "Hello"


def main() -> None:
    """Run the MCP server over Streamable HTTP."""
    mcp.run(transport="http", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
