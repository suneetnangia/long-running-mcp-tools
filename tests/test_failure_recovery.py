"""SIGKILL redelivery and duplicate approval against isolated, explicitly enabled Redis."""

import asyncio
import json
import os
import secrets
import signal
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp_tasks import call_tool_task
from mcp.types import ElicitResult
from redis.asyncio import Redis

from failure_probes import ProbeEvent, probe_keys
from lifecycle_client import get_task, task_result, update_task, wait_for_task
from lifecycle_processes import lifecycle_workspace, running_server

__all__ = ["lifecycle_workspace"]

pytestmark = pytest.mark.skipif(
    not os.getenv("MCP_TEST_REDIS_URL"),
    reason="Set MCP_TEST_REDIS_URL to a dedicated Redis/Valkey URL for failure experiments.",
)


async def read_events(redis: Redis, run_id: str) -> list[ProbeEvent]:
    events, _, _ = probe_keys(run_id)
    return [ProbeEvent.model_validate_json(value) for value in await redis.lrange(events, 0, -1)]


async def test_active_task_is_redelivered_after_sigkill(
    lifecycle_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_url = os.environ["MCP_TEST_REDIS_URL"]
    run_id = uuid4().hex
    queue_name = f"crash-{run_id}"
    monkeypatch.setenv("FASTMCP_TASKS_ENCRYPTION_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("FASTMCP_DOCKET_REDELIVERY_TIMEOUT", "PT2S")
    async with Redis.from_url(backend_url) as redis:
        try:
            async with running_server(
                lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
            ) as first:
                async with Client(first.url, mode="auto", timeout=5) as client:
                    task = await call_tool_task(client, "crash_probe", {"run_id": run_id})
                    created = task.create_result
                    async with asyncio.timeout(10):
                        while True:
                            before = await read_events(redis, run_id)
                            if [event.step for event in before if event.kind == "step"] == [
                                1,
                                2,
                                3,
                                4,
                            ]:
                                break
                            await asyncio.sleep(0.02)
                    assert (await get_task(client, created.task_id)).status == "working"
                assert {event.pid for event in before} == {first.process.pid}
                first.process.kill()
                await asyncio.wait_for(first.process.wait(), timeout=5)
                assert first.process.returncode == -signal.SIGKILL
                killed_at = monotonic()

            async with running_server(
                lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
            ) as second:
                assert first.process.pid != second.process.pid
                async with Client(second.url, mode="auto", timeout=5) as client:
                    recovered = await get_task(client, created.task_id)
                    assert recovered.task_id == created.task_id
                    assert recovered.created_at == created.created_at
                    final = await wait_for_task(
                        client, created.task_id, stop_at_input=False, timeout=20
                    )
                    result = task_result(final)
                    events = await read_events(redis, run_id)
                    steps = [event.step for event in events if event.kind == "step"]
                    starts = [event for event in events if event.kind == "start"]
                    assert len(starts) == 2
                    print(
                        json.dumps(
                            {
                                "experiment": "active-crash",
                                "task_id": created.task_id,
                                "first_pid": first.process.pid,
                                "replacement_pid": second.process.pid,
                                "worker_restart_seconds": round(
                                    starts[1].attempt_started_at - killed_at, 3
                                ),
                                "steps": steps,
                            }
                        )
                    )
                    assert final.task_id == created.task_id
                    assert {event.task_id for event in events} == {created.task_id}
                    assert [event.pid for event in starts] == [
                        first.process.pid,
                        second.process.pid,
                    ]
                    assert steps == [1, 2, 3, 4, *range(1, 11)]
                    assert result.structured_content == {
                        "task_id": created.task_id,
                        "attempt": 2,
                        "steps": list(range(1, 11)),
                    }
        finally:
            await redis.delete(*probe_keys(run_id))


async def test_duplicate_approvals_across_instances_have_one_effect(
    lifecycle_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_url = os.environ["MCP_TEST_REDIS_URL"]
    run_id = uuid4().hex
    queue_name = f"approval-race-{run_id}"
    monkeypatch.setenv("FASTMCP_TASKS_ENCRYPTION_KEY", secrets.token_urlsafe(32))
    async with Redis.from_url(backend_url) as redis:
        try:
            async with (
                running_server(
                    lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
                ) as first,
                running_server(
                    lifecycle_workspace, backend_url=backend_url, queue_name=queue_name
                ) as second,
                Client(first.url, mode="auto", timeout=5) as client_a,
                Client(second.url, mode="auto", timeout=5) as client_b,
            ):
                task = await call_tool_task(client_a, "approval_probe", {"run_id": run_id})
                waiting = await wait_for_task(client_a, task.task_id, timeout=10)
                assert waiting.status == "input_required"
                assert waiting.input_requests and len(waiting.input_requests) == 1
                assert (
                    await get_task(client_b, task.task_id)
                ).input_requests == waiting.input_requests
                input_id = next(iter(waiting.input_requests))
                responses = {input_id: ElicitResult(action="accept", content={"approved": True})}
                async with asyncio.timeout(10):
                    await asyncio.gather(
                        update_task(client_a, task.task_id, responses),
                        update_task(client_b, task.task_id, responses),
                    )
                final = await wait_for_task(client_b, task.task_id, stop_at_input=False, timeout=10)
                result = task_result(final)
                async with asyncio.timeout(10):
                    await asyncio.gather(
                        update_task(client_a, task.task_id, responses),
                        update_task(client_b, task.task_id, responses),
                    )
                    barriers = await asyncio.gather(
                        call_tool_task(client_a, "queued_probe", {}),
                        call_tool_task(client_b, "queued_probe", {}),
                    )
                    await asyncio.gather(*(barrier.result() for barrier in barriers))
                assert task_result(await get_task(client_a, task.task_id)) == result
                events = await read_events(redis, run_id)
                print(
                    json.dumps(
                        {
                            "experiment": "duplicate-approval",
                            "task_id": task.task_id,
                            "server_pids": [first.process.pid, second.process.pid],
                            "events": [event.kind for event in events],
                            "update_requests": 4,
                        }
                    )
                )
                assert [event.kind for event in events].count("start") == 2
                assert [event.kind for event in events].count("effect") == 1
                assert {event.task_id for event in events} == {task.task_id}
                assert result.structured_content == {"task_id": task.task_id, "effects": 1}
                _, attempts_key, effects_key = probe_keys(run_id)
                assert await redis.get(attempts_key) == b"2"
                assert await redis.get(effects_key) == b"1"
        finally:
            await redis.delete(*probe_keys(run_id))
