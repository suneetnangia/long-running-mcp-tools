"""Task-enabled FastMCP server with a configurable delayed greeting."""

import asyncio
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.utilities.tasks import TaskConfig
from fastmcp_tasks import TasksExtension
from pydantic import Field

from long_running_mcp_tools.adaptive_tasks import AdaptiveTasks, HideAdaptiveTwins

mcp = FastMCP("Long-Running MCP Tools")
mcp.add_extension(TasksExtension())

# AdaptiveTasks decides per call, based on how long the tool actually takes,
# whether to run it synchronously or promote it to a background task -- see
# long_running_mcp_tools/adaptive_tasks.py for why this needs its own
# extension (and a hidden twin tool) rather than plain `task=True`.
adaptive_tasks = AdaptiveTasks(grace_period=1.0)
mcp.add_extension(adaptive_tasks)
mcp.add_middleware(HideAdaptiveTwins())


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


@mcp.tool(task=TaskConfig(mode="optional"))
async def flexible_hello(
    delay_ms: Annotated[
        int,
        Field(
            ge=10,
            le=600_000,
            description="Delay in milliseconds, from 10 ms through 10 minutes.",
        ),
    ] = 1_000,
) -> str:
    """Return a greeting inline or as a caller-requested background task."""
    await asyncio.sleep(delay_ms / 1_000)
    return "Hello from a flexible task"


@adaptive_tasks.tool()
async def adaptive_delay(
    delay_ms: Annotated[
        int,
        Field(
            ge=10,
            le=600_000,
            description="Delay in milliseconds, from 10 ms through 10 minutes.",
        ),
    ] = 1_000,
) -> str:
    """Return a greeting, promoted to a background task if it runs long.

    Unlike `delayed_hello` (always a background task), this call only
    becomes one if it is still running after `adaptive_tasks.grace_period`.
    """
    await asyncio.sleep(delay_ms / 1_000)
    return "Hello from an adaptive task"


def main() -> None:
    """Run the MCP server over Streamable HTTP."""
    mcp.run(transport="http", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
