"""Shared pytest fixtures for the `long_running_mcp_tools` test suite."""

from typing import Any

import pytest
from fastmcp_tasks.creation import create_task as real_create_task

from long_running_mcp_tools import adaptive_tasks as adaptive_tasks_module


@pytest.fixture
def promoted_tool_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of the tools `AdaptiveTasks` has actually promoted to a background task.

    Patches `create_task` to record each call while still delegating to the
    real implementation, so a test can assert whether a call was promoted --
    directly, rather than inferring it from how long the call took.
    """
    promoted: list[str] = []

    async def spy(tool: Any, arguments: Any, context: Any) -> Any:
        promoted.append(tool.name)
        return await real_create_task(tool, arguments, context)

    monkeypatch.setattr(adaptive_tasks_module, "create_task", spy)
    return promoted
