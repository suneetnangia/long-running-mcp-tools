"""Test-only wire helpers and independent, scripted-stdin task client."""

import argparse
import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import ClientTransport
from fastmcp.exceptions import ToolError
from fastmcp_tasks import call_tool_task
from fastmcp_tasks.client_models import (
    CancelTaskRequest,
    CancelTaskRequestParams,
    ClientGetTaskResult,
    GetTaskRequest,
    GetTaskRequestParams,
    UpdateTaskRequest,
    UpdateTaskRequestParams,
)
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ElicitRequest, ElicitRequestFormParams, ElicitResult, Result


async def get_task[T: ClientTransport](client: Client[T], task_id: str) -> ClientGetTaskResult:
    return await client.session.send_request(
        GetTaskRequest(params=GetTaskRequestParams(task_id=task_id)), ClientGetTaskResult
    )


async def update_task[T: ClientTransport](
    client: Client[T], task_id: str, responses: dict[str, ElicitResult]
) -> None:
    await client.session.send_request(
        UpdateTaskRequest(
            params=UpdateTaskRequestParams(
                task_id=task_id,
                input_responses={
                    key: value.model_dump(by_alias=True, exclude_none=True)
                    for key, value in responses.items()
                },
            )
        ),
        Result,
    )


async def wait_for_task[T: ClientTransport](
    client: Client[T], task_id: str, *, stop_at_input: bool = True, timeout: float = 15
) -> ClientGetTaskResult:
    async with asyncio.timeout(timeout):
        while True:
            task = await get_task(client, task_id)
            if task.status in {"completed", "failed", "cancelled"} or (
                stop_at_input and task.status == "input_required"
            ):
                return task
            await asyncio.sleep(0.05)


def task_result(task: ClientGetTaskResult) -> CallToolResult:
    if task.status != "completed" or task.result is None:
        raise ToolError(f"Task {task.task_id} is {task.status}: {task.error}")
    result = CallToolResult.model_validate(task.result)
    if result.is_error:
        raise ToolError(f"Task {task.task_id} returned a tool error: {result.content}")
    return result


async def resume_task[T: ClientTransport](client: Client[T], task_id: str) -> CallToolResult:
    while True:
        snapshot = await wait_for_task(client, task_id)
        if snapshot.status != "input_required":
            return task_result(snapshot)
        if not snapshot.input_requests:
            raise ToolError("Missing input requests")
        responses: dict[str, ElicitResult] = {}
        for key, payload in snapshot.input_requests.items():
            request = ElicitRequest.model_validate(payload)
            if not isinstance(request.params, ElicitRequestFormParams):
                raise ToolError("Expected form-based approval")
            if request.params.requested_schema != {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
            }:
                raise ToolError("Unexpected approval schema")
            print(f"Approval required: {request.params.message}", file=sys.stderr, flush=True)
            try:
                answer = (await asyncio.to_thread(input)).strip().lower()
            except EOFError as exc:
                raise ToolError("No approval received; task remains pending") from exc
            if answer not in {"yes", "no"}:
                raise ToolError("Expected yes or no; task remains pending")
            responses[key] = ElicitResult(action="accept", content={"approved": answer == "yes"})
        await update_task(client, task_id, responses)


async def run_command(args: argparse.Namespace) -> None:
    async with Client(os.environ["MCP_SERVER_URL"], mode="auto", timeout=5) as client:
        if args.command == "submit":
            task = await call_tool_task(
                client,
                "deploy_batch",
                {"services": args.services, "per_service_delay_ms": args.per_service_delay_ms},
            )
            print(task.create_result.model_dump_json(by_alias=True, exclude_none=True), flush=True)
        elif args.command == "status":
            print((await get_task(client, args.task_id)).model_dump_json(by_alias=True), flush=True)
        elif args.command == "resume":
            print(
                (await resume_task(client, args.task_id)).model_dump_json(by_alias=True), flush=True
            )
        elif args.command == "cancel":
            await client.session.send_request(
                CancelTaskRequest(params=CancelTaskRequestParams(task_id=args.task_id)), Result
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--services", nargs="+", default=["api"])
    submit.add_argument("--per-service-delay-ms", type=int, default=200)
    for name in ("status", "resume", "cancel"):
        commands.add_parser(name).add_argument("task_id")
    try:
        asyncio.run(run_command(parser.parse_args()))
    except (MCPError, ToolError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
