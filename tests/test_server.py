"""Tests for the delayed hello MCP server."""

from datetime import timedelta

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


async def test_flexible_hello_runs_inline() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("flexible_hello", {"delay_ms": 10})

    assert result.data == "Hello from a flexible task"


async def test_flexible_hello_runs_as_background_task() -> None:
    async with Client(mcp, mode="auto") as client:
        task = await call_tool_task(client, "flexible_hello", {"delay_ms": 10})
        result = await task

    assert result.data == "Hello from a flexible task"


async def test_flexible_hello_configures_optional_task_poll_interval() -> None:
    tool = await mcp.get_tool("flexible_hello")

    assert tool is not None
    assert tool.task_config.mode == "optional"
    assert tool.task_config.poll_interval == timedelta(seconds=2)


async def test_flexible_hello_advertises_delay_bounds() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    flexible_hello = next(tool for tool in tools if tool.name == "flexible_hello")
    delay_schema = flexible_hello.input_schema["properties"]["delay_ms"]

    assert delay_schema["minimum"] == 10
    assert delay_schema["maximum"] == 600_000


async def test_delayed_hello_advertises_delay_bounds() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    delayed_hello = next(tool for tool in tools if tool.name == "delayed_hello")
    delay_schema = delayed_hello.input_schema["properties"]["delay_ms"]

    assert delay_schema["minimum"] == 10
    assert delay_schema["maximum"] == 600_000


async def test_adaptive_delay_hides_its_task_twin_from_listing() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    assert "adaptive_delay" in names
    assert "adaptive_delay__adaptive_task" not in names


async def test_adaptive_delay_fast_call_runs_inline(
    promoted_tool_names: list[str],
) -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("adaptive_delay", {"delay_ms": 50})

    assert result.data == "Hello from an adaptive task"
    assert promoted_tool_names == []


async def test_adaptive_delay_slow_call_is_promoted_and_still_resolves(
    promoted_tool_names: list[str],
) -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("adaptive_delay", {"delay_ms": 1_500})

    assert result.data == "Hello from an adaptive task"
    assert promoted_tool_names == ["adaptive_delay__adaptive_task"]
