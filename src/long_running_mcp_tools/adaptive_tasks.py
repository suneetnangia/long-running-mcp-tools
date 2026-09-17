"""A ``tools/call`` interceptor that decides, per call, whether a tool needs
to run as a background task -- instead of committing to "always sync" or
"always task" up front.

Register a tool with :meth:`AdaptiveTasks.tool` and its call races the tool's
real body against a grace period. A call that finishes quickly returns its
result directly, exactly like a normal tool. A call still running when the
grace period elapses is cancelled and handed off to
:func:`fastmcp_tasks.creation.create_task`, so the client keeps polling for
the eventual result instead of the request just hanging.

See https://gofastmcp.com/servers/tasks for FastMCP's background task model
and ``fastmcp.server.extensions`` (SEP-2133) for the ``tools/call``
interceptor this extension hooks into.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any, TypeVar

from fastmcp.server.extensions import ServerExtension
from fastmcp.server.middleware import Middleware
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from fastmcp_tasks.creation import create_task

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import mcp_types
    from fastmcp.server.context import Context
    from fastmcp.server.extensions import ToolCallContinuation, ToolCallOutcome
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.base import Tool

F = TypeVar("F", bound="Callable[..., Awaitable[Any]]")

#: Suffix marking a tool as an `AdaptiveTasks`-managed background twin. Shared
#: with `HideAdaptiveTwins`, which keeps twins out of `tools/list` by name.
TWIN_SUFFIX = "__adaptive_task"


class AdaptiveTasks(ServerExtension):
    """Race a tool call against a grace period, escalating to a background
    task only if it is still running once the grace period elapses.

    Register a tool with :meth:`tool` instead of ``@mcp.tool``. It creates two
    components: the public tool clients call (synchronous, ``task=False``)
    and a same-name-plus-suffix "twin" tool (``task=True``) that Docket knows
    how to run in the background. `intercept_tool_call` races the public
    tool's real body against ``grace_period``; if the body is still running
    when the grace period elapses, it is cancelled and the same call is
    handed to the twin via `create_task`, so the client transparently keeps
    polling for the final result instead of the request just hanging.

    Two tools are needed because a single ``task=True`` tool is already fully
    owned by ``TasksExtension``: whenever the calling client has negotiated
    the tasks capability (every ``fastmcp`` client does, automatically), the
    ``optional``/``required`` task modes make ``TasksExtension`` promote the
    call immediately, before the tool body ever runs -- there is no grace
    period left to race. Keeping the public tool ``task=False`` means
    ``TasksExtension`` always leaves it alone, so this extension is the only
    thing deciding whether a given call is promoted.

    A promoted call is only safe for a client that has itself declared the
    tasks capability for the request -- otherwise it cannot parse the
    ``CreateTaskResult`` the server sends back in place of the tool's real
    result (an unhandled response shape, not a graceful degrade: an
    unsolicited ``CreateTaskResult`` fails client-side response validation).
    `intercept_tool_call` checks that per request and, when the client has
    not opted in, simply lets the call keep running inline with no grace
    period enforced rather than risk promoting it.

    Requires ``TasksExtension`` to also be registered on the server (for the
    Docket backend, and the ``tasks/get``/``tasks/update``/``tasks/cancel``
    methods a promoted call is polled through), and requires
    `HideAdaptiveTwins` middleware if twin tools should stay out of
    ``tools/list``.
    """

    identifier = "dev.long-running-mcp-tools/adaptive-tasks"

    def __init__(self, grace_period: float = 2.0) -> None:
        self.grace_period = grace_period
        self._twins: dict[str, Tool] = {}

    def tool(self, *, name: str | None = None, **tool_kwargs: Any) -> Callable[[F], F]:
        """Decorator: register ``fn`` as an adaptive tool.

        Accepts the same keyword arguments as ``@mcp.tool`` (besides
        ``task``, which this extension controls). Registers the public tool
        plus its background-capable twin on the bound server (see
        `ServerExtension.server`), so this extension must already be
        registered with ``FastMCP.add_extension()`` before a tool is declared
        with this decorator.
        """
        if "task" in tool_kwargs:
            raise ValueError(
                "AdaptiveTasks.tool() manages the `task` option itself; do not pass it explicitly."
            )

        def decorator(fn: F) -> F:
            public_name = name or fn.__name__
            self.server.add_tool(FunctionTool.from_function(fn, name=public_name, **tool_kwargs))
            twin = self.server.add_tool(
                FunctionTool.from_function(fn, name=f"{public_name}{TWIN_SUFFIX}", task=True)
            )
            self._twins[public_name] = twin
            return fn

        return decorator

    async def intercept_tool_call(
        self,
        params: mcp_types.CallToolRequestParams,
        context: Context,
        call_next: ToolCallContinuation,
    ) -> ToolCallOutcome:
        twin = self._twins.get(params.name)
        if twin is None:
            # Not one of ours -- nothing to race.
            return await call_next()

        if context.client_extension_settings(TASKS_EXTENSION_ID) is None:
            # The client can't parse a CreateTaskResult, so there is nothing
            # safe to promote to; just let the call run to completion.
            return await call_next()

        work = asyncio.ensure_future(call_next())
        try:
            return await asyncio.wait_for(work, timeout=self.grace_period)
        except TimeoutError:
            # Don't shield-and-abandon: cancel the in-flight attempt so it
            # doesn't keep running unobserved alongside the promoted task.
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
            return await create_task(twin, params.arguments, context)


class HideAdaptiveTwins(Middleware):
    """Keeps `AdaptiveTasks` twin tools out of ``tools/list``.

    A twin tool must stay resolvable by name (``FastMCP.get_tool``) for
    `create_task` and the worker to run it, so it can't be disabled outright
    -- disabling also hides a tool from ``get_tool``, which would break
    ``tasks/get`` once the promoted call completes. Filtering the
    ``tools/list`` response here, instead, only affects discovery: the twin
    stays reachable by name, it just isn't advertised.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [tool for tool in tools if not tool.name.endswith(TWIN_SUFFIX)]
