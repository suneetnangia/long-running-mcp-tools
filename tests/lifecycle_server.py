"""Isolated test-only adaptive MRTR server; no application server or cloud effects."""

import asyncio
import os
import sys

import uvicorn
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tasks import TaskConfig
from fastmcp_tasks import TasksExtension
from fastmcp_tasks.context import get_task_context
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult

from failure_probes import register_probes
from long_running_mcp_tools.adaptive_tasks import AdaptiveTasks, HideAdaptiveTwins

GRACE_PERIOD = 0.05


def approval_request() -> InputRequiredResult:
    return InputRequiredResult(
        request_state="plan-ready",
        input_requests={
            "approval": ElicitRequest(
                params=ElicitRequestFormParams(
                    message="Approve the simulated deployment?",
                    requested_schema={
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                    },
                )
            )
        },
    )


def build_server() -> FastMCP:
    server = FastMCP("Isolated lifecycle probes")
    server.add_extension(TasksExtension())
    adaptive = AdaptiveTasks(grace_period=GRACE_PERIOD)
    server.add_extension(adaptive)
    server.add_middleware(HideAdaptiveTwins())

    @adaptive.tool()
    async def deploy_batch(
        services: list[str], ctx: Context, per_service_delay_ms: int = 200
    ) -> dict[str, object] | InputRequiredResult:
        if ctx.request_state is None:
            # The public attempt MUST exceed grace before exposing an input request.
            await asyncio.sleep(max(per_service_delay_ms / 1_000, GRACE_PERIOD * 4))
            if get_task_context() is None:
                raise ToolError("Approval must be reached in the promoted worker")
            return approval_request()
        if ctx.request_state != "plan-ready" or ctx.input_responses is None:
            raise ToolError("Approval continuation lost its durable plan state")
        response = ctx.input_responses.get("approval")
        approved = (
            isinstance(response, ElicitResult)
            and response.action == "accept"
            and response.content is not None
            and response.content.get("approved") is True
        )
        return {
            "status": "completed" if approved else "declined",
            "environment": "production",
            "deployed_services": services if approved else [],
        }

    @server.tool(task=TaskConfig(mode="required"))
    async def queued_probe(delay_ms: int = 50) -> dict[str, object]:
        context = get_task_context()
        if context is None:
            raise ToolError("Queue probe must execute in a worker")
        await asyncio.sleep(delay_ms / 1_000)
        return {
            "task_id": context.task_id,
            "pid": os.getpid(),
            "concurrency": os.environ["FASTMCP_DOCKET_CONCURRENCY"],
        }

    register_probes(server, adaptive)
    return server


def main() -> None:
    uvicorn.run(
        build_server().http_app(), fd=int(sys.argv[1]), log_level="warning", access_log=False
    )


if __name__ == "__main__":
    main()
