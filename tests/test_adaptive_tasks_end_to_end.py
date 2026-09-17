"""End-to-end coverage for `AdaptiveTasks` through a real server.

`test_adaptive_tasks.py` stubs `call_next`/`create_task` to test
`intercept_tool_call`'s decision logic in isolation. These tests instead build
a throwaway server -- wired the same way as `long_running_mcp_tools.server` --
and drive it with a real `fastmcp.Client`, so promotion, the Docket-backed
background execution, and `tasks/get` polling are all exercised for real.

Naming note: "raises"/"error" below refers to the *tool call's outcome*
(the tool itself raises an exception) -- every test in this file is expected
to, and does, PASS; a test failing would mean the described behavior broke.

Each test also asserts on `promoted_tool_names` (see `conftest.py`) rather
than inferring promotion from how long the call took, so a fast call that
promotes unexpectedly (or a slow call that never does) fails loudly instead
of just running slow.
"""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp_tasks import TasksExtension, call_tool_task

from long_running_mcp_tools.adaptive_tasks import AdaptiveTasks, HideAdaptiveTwins

GRACE_PERIOD = 0.2
FAST_DELAY_MS = 10
SLOW_DELAY_MS = 500  # comfortably past GRACE_PERIOD, so the call is promoted
TWIN_NAME = "maybe_fail__adaptive_task"


def _build_server() -> FastMCP:
    """A throwaway server wired the same way as `long_running_mcp_tools.server`."""
    mcp = FastMCP("AdaptiveTasks end-to-end test server")
    mcp.add_extension(TasksExtension())
    adaptive = AdaptiveTasks(grace_period=GRACE_PERIOD)
    mcp.add_extension(adaptive)
    mcp.add_middleware(HideAdaptiveTwins())

    @adaptive.tool()
    async def maybe_fail(delay_ms: int = 10, should_fail: bool = False) -> str:
        await asyncio.sleep(delay_ms / 1_000)
        if should_fail:
            raise ValueError("intentional failure")
        return f"ok after {delay_ms}ms"

    return mcp


async def test_call_within_grace_period_succeeds_without_promotion(
    promoted_tool_names: list[str],
) -> None:
    """A quick, successful call returns its result directly, never promoted."""
    mcp = _build_server()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "maybe_fail", {"delay_ms": FAST_DELAY_MS, "should_fail": False}
        )

    assert result.data == f"ok after {FAST_DELAY_MS}ms"
    assert promoted_tool_names == []


async def test_call_within_grace_period_raises_without_promotion(
    promoted_tool_names: list[str],
) -> None:
    """A quick call whose tool body raises propagates that error directly,
    with no background task ever created."""
    mcp = _build_server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="intentional failure"):
            await client.call_tool("maybe_fail", {"delay_ms": FAST_DELAY_MS, "should_fail": True})

    assert promoted_tool_names == []


async def test_call_exceeding_grace_period_is_promoted_and_succeeds(
    promoted_tool_names: list[str],
) -> None:
    """A slow call is promoted to a background task, and its success still
    resolves back to the caller once polled to completion."""
    mcp = _build_server()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "maybe_fail", {"delay_ms": SLOW_DELAY_MS, "should_fail": False}
        )

    assert result.data == f"ok after {SLOW_DELAY_MS}ms"
    assert promoted_tool_names == [TWIN_NAME]


async def test_call_exceeding_grace_period_is_promoted_and_raises(
    promoted_tool_names: list[str],
) -> None:
    """A slow call is promoted to a background task, and an error raised in
    that background execution still surfaces back to the caller once
    polled -- rather than being lost or hanging."""
    mcp = _build_server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="intentional failure"):
            await client.call_tool("maybe_fail", {"delay_ms": SLOW_DELAY_MS, "should_fail": True})

    assert promoted_tool_names == [TWIN_NAME]


async def test_tasks_get_on_an_ongoing_task_reports_working(
    promoted_tool_names: list[str],
) -> None:
    """`tasks/get` on a promoted task that has not finished yet reports
    `working`, not one of the terminal statuses (`completed`/`failed`/
    `cancelled`) -- i.e. polling mid-flight does not itself force or fake
    completion.

    Uses `call_tool_task` (rather than `client.call_tool`) because that is
    the one client entry point that returns a handle to a still-running task
    instead of transparently polling it to completion, so the test can
    inspect the task while it is genuinely ongoing.
    """
    mcp = _build_server()
    async with Client(mcp, mode="auto") as client:
        task = await call_tool_task(
            client, "maybe_fail", {"delay_ms": SLOW_DELAY_MS, "should_fail": False}
        )

        status = await task.status()
        assert status.status == "working"
        assert status.task_id == task.task_id

        # Drive it to completion so the test doesn't leave a dangling
        # background execution, and confirm the eventual result is correct.
        result = await task

    assert result.data == f"ok after {SLOW_DELAY_MS}ms"
    assert promoted_tool_names == [TWIN_NAME]
