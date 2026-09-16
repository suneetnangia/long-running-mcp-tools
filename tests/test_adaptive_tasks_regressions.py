"""Known correctness gaps in the twin-based decorator, not implementation fixes.

Strict, exception-specific xfails document only the two reproduced symptoms.
Run with --runxfail to expose their assertions; unrelated errors still fail.
"""

import asyncio

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp_tasks import TasksExtension, call_tool_task
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult

from long_running_mcp_tools.adaptive_tasks import AdaptiveTasks, HideAdaptiveTwins


class WrongPromotedVersion(AssertionError):
    """The requested version changed when the tool was promoted."""


class LostContinuationInput(AssertionError):
    """A previously answered MRTR continuation asked for input again."""


def build_server() -> tuple[FastMCP, AdaptiveTasks]:
    server = FastMCP("Twin regression probes")
    server.add_extension(TasksExtension())
    adaptive = AdaptiveTasks(grace_period=0.1)
    server.add_extension(adaptive)
    server.add_middleware(HideAdaptiveTwins())
    return server, adaptive


@pytest.mark.xfail(
    strict=True,
    raises=WrongPromotedVersion,
    reason="REG-1: twins and their name-keyed mapping do not preserve the requested tool version",
)
async def test_requested_version_is_preserved_after_promotion(
    promoted_tool_names: list[str],
) -> None:
    server, adaptive = build_server()
    calls: list[str] = []

    @adaptive.tool(name="versioned", version="1.0")
    async def version_one(delay_ms: int) -> str:
        calls.append("v1")
        await asyncio.sleep(delay_ms / 1_000)
        return "v1"

    @adaptive.tool(name="versioned", version="2.0")
    async def version_two(delay_ms: int) -> str:
        calls.append("v2")
        await asyncio.sleep(delay_ms / 1_000)
        return "v2"

    async with asyncio.timeout(10), Client(server, mode="auto", timeout=5) as client:
        inline = await client.call_tool("versioned", {"delay_ms": 1}, version="1.0")
        assert inline.data == "v1"
        assert calls == ["v1"]
        assert promoted_tool_names == []
        calls.clear()

        task = await call_tool_task(client, "versioned", {"delay_ms": 300}, version="1.0")
        result = await task
        final = await task.status()
        assert final.status == "completed"
        assert final.task_id == task.task_id
        assert promoted_tool_names == ["versioned__adaptive_task"]

    if result.data != "v1":
        # Only the known, measured mismatch is expected; other failures stay red.
        assert result.data == "v2"
        assert calls == ["v1", "v2"]
        raise WrongPromotedVersion(
            f"Requested version 1.0: inline returned {inline.data!r}, "
            f"promoted returned {result.data!r}; invocations={calls!r}"
        )
    assert calls == ["v1", "v1"]


@pytest.mark.xfail(
    strict=True,
    raises=LostContinuationInput,
    reason="REG-2: foreground MRTR continuation loses input responses/state on promotion",
)
async def test_answered_mrtr_continuation_is_not_prompted_again(
    promoted_tool_names: list[str],
) -> None:
    server, adaptive = build_server()
    prompts: list[str] = []
    observed: list[tuple[bool, str | None]] = []

    @adaptive.tool()
    async def approve_then_work(ctx: Context, delay_ms: int) -> str | InputRequiredResult:
        observed.append((ctx.input_responses is not None, ctx.request_state))
        if ctx.input_responses is None:
            return InputRequiredResult(
                request_state="approval-context",
                input_requests={
                    "approval": ElicitRequest(
                        params=ElicitRequestFormParams(
                            message="Approve the simulated operation?",
                            requested_schema={
                                "type": "object",
                                "properties": {"approved": {"type": "boolean"}},
                                "required": ["approved"],
                            },
                        )
                    )
                },
            )
        assert ctx.request_state == "approval-context"
        answer = ctx.input_responses["approval"]
        assert isinstance(answer, ElicitResult)
        assert answer.action == "accept"
        assert answer.content == {"approved": True}
        await asyncio.sleep(delay_ms / 1_000)
        return "approved"

    async def approve(message: str, *_: object) -> dict[str, bool]:
        prompts.append(message)
        return {"approved": True}

    async with (
        asyncio.timeout(10),
        Client(server, mode="auto", timeout=5, elicitation_handler=approve) as client,
    ):
        inline = await client.call_tool("approve_then_work", {"delay_ms": 1})
        assert inline.data == "approved"
        assert len(prompts) == 1
        assert observed == [(False, None), (True, "approval-context")]
        assert promoted_tool_names == []
        prompts.clear()
        observed.clear()

        result = await client.call_tool("approve_then_work", {"delay_ms": 300})
        assert result.data == "approved"
        assert promoted_tool_names == ["approve_then_work__adaptive_task"]

    if len(prompts) != 1:
        assert len(prompts) == 2
        assert observed == [
            (False, None),
            (True, "approval-context"),
            (False, None),
            (True, "approval-context"),
        ]
        raise LostContinuationInput(
            f"Expected one prompt, got {len(prompts)}; "
            f"(has_input, request_state) on each invocation={observed!r}"
        )
