"""Bounded subprocess harness using reserved loopback sockets and repository-local logs."""

import asyncio
import os
import shutil
import socket
import sys
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastmcp import Client

from long_running_mcp_tools.adaptive_tasks import TWIN_SUFFIX


@pytest.fixture
def lifecycle_workspace() -> Iterator[Path]:
    directory = Path(__file__).parent / f".lifecycle-{uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


@dataclass(frozen=True)
class RunningServer:
    url: str
    process: asyncio.subprocess.Process


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)


@asynccontextmanager
async def running_server(
    workspace: Path, *, backend_url: str = "memory://", queue_name: str | None = None
) -> AsyncGenerator[RunningServer]:
    log_path = workspace / f"server-{uuid4().hex}.log"
    with socket.socket() as listener, log_path.open("w") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("lifecycle_server.py")),
            str(listener.fileno()),
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
            env={
                **os.environ,
                "FASTMCP_DOCKET_URL": backend_url,
                "FASTMCP_DOCKET_NAME": queue_name or f"lifecycle-{uuid4().hex}",
                "FASTMCP_DOCKET_CONCURRENCY": "1",
            },
        )
        try:
            async with asyncio.timeout(15):
                while True:
                    if process.returncode is not None:
                        pytest.fail(f"Server failed to start:\n{log_path.read_text()}")
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                    except ConnectionRefusedError:
                        await asyncio.sleep(0.05)
                    else:
                        writer.close()
                        await writer.wait_closed()
                        break
            url = f"http://127.0.0.1:{port}/mcp"
            async with Client(url, mode="auto", timeout=5) as client:
                tools = await client.list_tools()
                assert any(tool.name == "deploy_batch" for tool in tools)
                assert not any(tool.name.endswith(TWIN_SUFFIX) for tool in tools)
            yield RunningServer(url, process)
        finally:
            await stop_process(process)


async def start_cli(url: str, *arguments: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("lifecycle_client.py")),
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "MCP_SERVER_URL": url},
    )


async def cli(url: str, *arguments: str, stdin: bytes = b"") -> tuple[int, bytes, bytes]:
    process = await start_cli(url, *arguments)
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout=20)
        assert process.returncode is not None
        return process.returncode, out, err
    finally:
        await stop_process(process)
