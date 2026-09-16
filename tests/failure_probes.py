"""Redis-backed evidence of attempts and simulated effects, registered only in tests."""

import asyncio
import os
from time import monotonic
from typing import Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tasks import TaskConfig
from fastmcp_tasks.context import get_task_context
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from long_running_mcp_tools.adaptive_tasks import AdaptiveTasks


class ProbeEvent(BaseModel):
    kind: Literal["start", "step", "effect"]
    task_id: str
    attempt: int
    pid: int
    attempt_started_at: float = Field(default_factory=monotonic)
    step: int | None = None


def probe_keys(run_id: str) -> tuple[str, str, str]:
    prefix = f"long-running-mcp-tools:lifecycle:{run_id}"
    return f"{prefix}:events", f"{prefix}:attempts", f"{prefix}:effects"


async def record(redis: Redis, run_id: str, event: ProbeEvent) -> None:
    events, _, _ = probe_keys(run_id)
    await redis.rpush(events, event.model_dump_json())
    await redis.expire(events, 900)


async def begin_attempt(redis: Redis, run_id: str) -> ProbeEvent:
    context = get_task_context()
    if context is None:
        raise ToolError("Failure probes must execute in a task worker")
    _, attempts, _ = probe_keys(run_id)
    attempt = await redis.incr(attempts)
    await redis.expire(attempts, 900)
    event = ProbeEvent(kind="start", task_id=context.task_id, attempt=attempt, pid=os.getpid())
    await record(redis, run_id, event)
    return event


def register_probes(server: FastMCP, adaptive: AdaptiveTasks) -> None:
    @server.tool(task=TaskConfig(mode="required"))
    async def crash_probe(run_id: str) -> dict[str, object]:
        # Required mode isolates worker redelivery from adaptive grace cancellation.
        async with Redis.from_url(os.environ["FASTMCP_DOCKET_URL"]) as redis:
            attempt = await begin_attempt(redis, run_id)
            for step in range(1, 11):
                await record(
                    redis, run_id, attempt.model_copy(update={"kind": "step", "step": step})
                )
                if attempt.attempt == 1 and step == 4:
                    await asyncio.Event().wait()
                await asyncio.sleep(0.02)
        return {"task_id": attempt.task_id, "attempt": attempt.attempt, "steps": list(range(1, 11))}

    @adaptive.tool()
    async def approval_probe(run_id: str, ctx: Context) -> dict[str, object] | InputRequiredResult:
        if ctx.request_state is None:
            # No ledger effects or input request until after adaptive promotion.
            await asyncio.sleep(max(adaptive.grace_period * 4, 0.2))
        async with Redis.from_url(os.environ["FASTMCP_DOCKET_URL"]) as redis:
            attempt = await begin_attempt(redis, run_id)
            if ctx.input_responses is None:
                return InputRequiredResult(
                    request_state="awaiting-approval",
                    input_requests={
                        "approval": ElicitRequest(
                            params=ElicitRequestFormParams(
                                message="Approve the test-only counter increment?",
                                requested_schema={
                                    "type": "object",
                                    "properties": {"approved": {"type": "boolean"}},
                                    "required": ["approved"],
                                },
                            )
                        )
                    },
                )
            if ctx.request_state != "awaiting-approval":
                raise ToolError("Approval continuation lost its request state")
            answer = ctx.input_responses.get("approval")
            if (
                not isinstance(answer, ElicitResult)
                or answer.action != "accept"
                or answer.content is None
                or answer.content.get("approved") is not True
            ):
                raise ToolError("Counter probe requires explicit approval")
            _, _, effects_key = probe_keys(run_id)
            # Deliberately not idempotent: duplicate continuation must be observable.
            effects = await redis.incr(effects_key)
            await redis.expire(effects_key, 900)
            await record(redis, run_id, attempt.model_copy(update={"kind": "effect"}))
            await asyncio.sleep(0.2)
        return {"task_id": attempt.task_id, "effects": effects}
