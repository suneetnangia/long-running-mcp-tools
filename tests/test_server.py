"""Tests for the delayed hello MCP server."""

from fastmcp import Client
from fastmcp_tasks import call_tool_task

from long_running_mcp_tools.server import mcp


async def test_delayed_hello_returns_greeting() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("delayed_hello", {"delay_ms": 10})

    assert result.data == "Hello"


async def test_delayed_hello_runs_as_background_task() -> None:
    async with Client(mcp, mode="auto") as client:
        task = await call_tool_task(client, "delayed_hello", {"delay_ms": 10})
        result = await task

    assert result.data == "Hello"


async def test_delayed_hello_advertises_delay_bounds() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    delayed_hello = next(tool for tool in tools if tool.name == "delayed_hello")
    delay_schema = delayed_hello.input_schema["properties"]["delay_ms"]

    assert delay_schema["minimum"] == 10
    assert delay_schema["maximum"] == 600_000
