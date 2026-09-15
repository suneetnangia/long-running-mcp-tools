"""Unit tests for the `AdaptiveTasks` tools/call interceptor.

These exercise `AdaptiveTasks.intercept_tool_call` directly against stub
`Context`/`call_next` doubles, independent of a live `FastMCP` server or
Docket backend, since `intercept_tool_call` only ever reads
`context.client_extension_settings(...)` off the context it is given.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from long_running_mcp_tools import adaptive_tasks as adaptive_tasks_module
from long_running_mcp_tools.adaptive_tasks import AdaptiveTasks


class _StubContext:
    """A minimal stand-in for `fastmcp.server.context.Context`."""

    def __init__(self, *, opted_in: bool) -> None:
        self._opted_in = opted_in

    def client_extension_settings(self, identifier: str) -> dict[str, Any] | None:
        return {} if self._opted_in else None


def _params(name: str, **arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(name=name, arguments=arguments)


def _fail_if_called(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    async def fake_create_task(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(message)

    monkeypatch.setattr(adaptive_tasks_module, "create_task", fake_create_task)


async def _invoke(adaptive: AdaptiveTasks, params: Any, context: Any, call_next: Any) -> Any:
    """Call `intercept_tool_call` against permissively-typed test doubles."""
    return await adaptive.intercept_tool_call(params, context, call_next)


async def test_unrelated_tool_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool with no registered twin is never raced or promoted."""
    adaptive = AdaptiveTasks(grace_period=0.01)
    _fail_if_called(monkeypatch, "create_task should not run for an unmanaged tool")

    async def call_next() -> str:
        return "ok"

    result = await _invoke(adaptive, _params("other"), _StubContext(opted_in=True), call_next)

    assert result == "ok"


async def test_fast_call_returns_directly_without_promoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that finishes within the grace period is not promoted."""
    adaptive = AdaptiveTasks(grace_period=1.0)
    adaptive._twins["slow"] = object()  # type: ignore[assignment]
    _fail_if_called(monkeypatch, "create_task should not run for a fast call")

    async def call_next() -> str:
        return "done"

    result = await _invoke(
        adaptive, _params("slow", delay_ms=1), _StubContext(opted_in=True), call_next
    )

    assert result == "done"


async def test_slow_call_promotes_when_client_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call still running past the grace period is promoted to the twin."""
    adaptive = AdaptiveTasks(grace_period=0.02)
    twin = object()
    adaptive._twins["slow"] = twin  # type: ignore[assignment]

    promoted: list[tuple[Any, Any, Any]] = []

    async def fake_create_task(tool: Any, arguments: Any, context: Any) -> str:
        promoted.append((tool, arguments, context))
        return "promoted"

    monkeypatch.setattr(adaptive_tasks_module, "create_task", fake_create_task)

    async def call_next() -> str:
        await asyncio.sleep(1.0)
        return "too-late"

    context = _StubContext(opted_in=True)
    result = await _invoke(adaptive, _params("slow", delay_ms=999), context, call_next)

    assert result == "promoted"
    assert promoted == [(twin, {"delay_ms": 999}, context)]


async def test_fast_call_error_propagates_without_promoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that fails within the grace period raises -- it is not promoted."""
    adaptive = AdaptiveTasks(grace_period=1.0)
    adaptive._twins["slow"] = object()  # type: ignore[assignment]
    _fail_if_called(monkeypatch, "create_task should not run when the call already failed")

    async def call_next() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await _invoke(adaptive, _params("slow", delay_ms=1), _StubContext(opted_in=True), call_next)


async def test_timed_out_call_is_cancelled_not_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-flight attempt is cancelled on promotion, not left running."""
    adaptive = AdaptiveTasks(grace_period=0.02)
    adaptive._twins["slow"] = object()  # type: ignore[assignment]

    async def fake_create_task(*_args: Any, **_kwargs: Any) -> str:
        return "promoted"

    monkeypatch.setattr(adaptive_tasks_module, "create_task", fake_create_task)

    completed = False

    async def call_next() -> str:
        nonlocal completed
        await asyncio.sleep(0.2)
        completed = True
        return "late"

    await _invoke(adaptive, _params("slow"), _StubContext(opted_in=True), call_next)
    await asyncio.sleep(0.3)

    assert completed is False


async def test_slow_call_without_opt_in_runs_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a tasks-capable client, the call just runs -- no promotion."""
    adaptive = AdaptiveTasks(grace_period=0.01)
    adaptive._twins["slow"] = object()  # type: ignore[assignment]
    _fail_if_called(monkeypatch, "create_task should not run for a client that did not opt in")

    async def call_next() -> str:
        await asyncio.sleep(0.05)
        return "finished-late-but-safely"

    result = await _invoke(
        adaptive, _params("slow", delay_ms=999), _StubContext(opted_in=False), call_next
    )

    assert result == "finished-late-but-safely"


async def test_promotion_failure_propagates_and_still_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `create_task` itself fails (e.g. the Docket backend is down), the
    error propagates instead of being swallowed -- and the original in-flight
    attempt has still been cancelled rather than left running."""
    adaptive = AdaptiveTasks(grace_period=0.02)
    adaptive._twins["slow"] = object()  # type: ignore[assignment]

    async def failing_create_task(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("docket unavailable")

    monkeypatch.setattr(adaptive_tasks_module, "create_task", failing_create_task)

    completed = False

    async def call_next() -> str:
        nonlocal completed
        await asyncio.sleep(0.2)
        completed = True
        return "late"

    with pytest.raises(RuntimeError, match="docket unavailable"):
        await _invoke(adaptive, _params("slow"), _StubContext(opted_in=True), call_next)

    await asyncio.sleep(0.3)
    assert completed is False
