"""Real HTTP, independent client processes, and adaptive task approval continuations."""

import asyncio
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp_tasks import call_tool_task
from fastmcp_tasks.client_models import ClientCreateTaskResult, ClientGetTaskResult
from mcp.types import CallToolResult

from lifecycle_client import get_task, task_result, wait_for_task
from lifecycle_processes import (
    cli,
    lifecycle_workspace,
    running_server,
    start_cli,
    stop_process,
)

__all__ = ["lifecycle_workspace"]


@pytest.mark.parametrize("answer", ["yes", "no"])
async def test_disconnected_client_and_single_worker_approval(
    lifecycle_workspace: Path, answer: str
) -> None:
    async with running_server(lifecycle_workspace) as server:
        code, out, err = await cli(server.url, "submit", "--services", "api", "worker", "web")
        assert code == 0, err.decode()
        created = ClientCreateTaskResult.model_validate_json(out)
        assert created.status == "working"
        # communicate() has waited for submit process A to exit before starting B.
        code, out, err = await cli(server.url, "status", created.task_id)
        assert code == 0, err.decode()
        assert ClientGetTaskResult.model_validate_json(out).task_id == created.task_id

        resume = await start_cli(server.url, "resume", created.task_id)
        try:
            assert resume.stderr is not None
            async with asyncio.timeout(15):
                while True:
                    line = await resume.stderr.readline()
                    assert line, "Resume process exited without requesting approval"
                    if b"Approval required:" in line:
                        break
            async with Client(server.url, mode="auto", timeout=5) as observer:
                waiting = await get_task(observer, created.task_id)
                assert waiting.status == "input_required"
                assert waiting.input_requests
                # Required mode plus worker-only context proves this isn't a fast inline call.
                other = await call_tool_task(observer, "queued_probe", {})
                assert other.task_id != created.task_id
                queued_result = task_result(
                    await wait_for_task(observer, other.task_id, stop_at_input=False, timeout=5)
                )
                assert queued_result.structured_content == {
                    "task_id": other.task_id,
                    "pid": server.process.pid,
                    "concurrency": "1",
                }
                assert (await get_task(observer, created.task_id)).status == "input_required"
                assert resume.returncode is None
            stdout, stderr = await asyncio.wait_for(
                resume.communicate(f"{answer}\n".encode()), timeout=10
            )
            assert resume.returncode == 0, stderr.decode()
            final = CallToolResult.model_validate_json(stdout)
            assert not final.is_error
            assert final.structured_content == {
                "status": "completed" if answer == "yes" else "declined",
                "environment": "production",
                "deployed_services": ["api", "worker", "web"] if answer == "yes" else [],
            }
        finally:
            await stop_process(resume)

        code, out, err = await cli(server.url, "status", created.task_id)
        assert code == 0, err.decode()
        terminal = ClientGetTaskResult.model_validate_json(out)
        assert terminal.task_id == created.task_id
        assert terminal.created_at == created.created_at
        assert task_result(terminal) == final
        code, repeated, err = await cli(server.url, "resume", created.task_id)
        assert code == 0, err.decode()
        assert b"Approval required:" not in err
        assert json.loads(repeated) == json.loads(stdout)


async def test_eof_leaves_task_pending_and_cancel_works_from_new_client(
    lifecycle_workspace: Path,
) -> None:
    async with running_server(lifecycle_workspace) as server:
        code, out, err = await cli(server.url, "submit")
        assert code == 0, err.decode()
        created = ClientCreateTaskResult.model_validate_json(out)
        code, _, err = await cli(server.url, "resume", created.task_id)
        assert code == 1
        assert b"No approval received" in err
        async with Client(server.url, mode="auto", timeout=5) as observer:
            assert (await get_task(observer, created.task_id)).status == "input_required"
        code, _, err = await cli(server.url, "cancel", created.task_id)
        assert code == 0, err.decode()
        async with Client(server.url, mode="auto", timeout=5) as observer:
            assert (await wait_for_task(observer, created.task_id, timeout=5)).status == "cancelled"
        code, _, err = await cli(server.url, "resume", created.task_id)
        assert code == 1
        assert b"cancelled" in err


async def test_unknown_task_fails_without_resubmission(lifecycle_workspace: Path) -> None:
    async with running_server(lifecycle_workspace) as server:
        code, _, err = await cli(server.url, "resume", "does-not-exist")
    assert code == 1
    assert b"does-not-exist" in err


@pytest.mark.skipif(
    not os.getenv("MCP_TEST_REDIS_URL"),
    reason="Set MCP_TEST_REDIS_URL to a dedicated Redis/Valkey URL for process replacement.",
)
async def test_waiting_task_survives_server_replacement(
    lifecycle_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_url = os.environ["MCP_TEST_REDIS_URL"]
    queue_name = f"lifecycle-{uuid4().hex}"
    monkeypatch.setenv("FASTMCP_TASKS_ENCRYPTION_KEY", secrets.token_urlsafe(32))
    async with running_server(
        lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
    ) as first:
        code, out, err = await cli(first.url, "submit")
        assert code == 0, err.decode()
        created = ClientCreateTaskResult.model_validate_json(out)
        async with Client(first.url, mode="auto", timeout=5) as client:
            waiting = await wait_for_task(client, created.task_id, timeout=10)
            assert waiting.status == "input_required"

    assert first.process.returncode is not None
    async with running_server(
        lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
    ) as second:
        assert first.process.pid != second.process.pid
        async with Client(second.url, mode="auto", timeout=5) as client:
            recovered = await get_task(client, created.task_id)
            assert recovered.task_id == created.task_id
            assert recovered.created_at == created.created_at
            assert recovered.status == "input_required"
            assert recovered.input_requests == waiting.input_requests
        code, out, err = await cli(second.url, "resume", created.task_id, stdin=b"yes\n")
        assert code == 0, err.decode()
        result = CallToolResult.model_validate_json(out)
        assert result.structured_content == {
            "status": "completed",
            "environment": "production",
            "deployed_services": ["api"],
        }
        async with Client(second.url, mode="auto", timeout=5) as client:
            terminal = await get_task(client, created.task_id)
            assert terminal.task_id == created.task_id
            assert terminal.created_at == created.created_at
            assert task_result(terminal) == result

    # Stored completed results also survive replacement, not just input-required state.
    async with (
        running_server(
            lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
        ) as third,
        Client(third.url, mode="auto", timeout=5) as client,
    ):
        persisted = await get_task(client, created.task_id)
        assert persisted.task_id == created.task_id
        assert persisted.created_at == created.created_at
        assert task_result(persisted) == result
