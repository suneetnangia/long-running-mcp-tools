---
title: Long-Running MCP Tools
description: Examples of inline, background, and adaptive long-running FastMCP tools
---

Model Context Protocol (MCP) tools often wrap work that cannot finish within a
single short-lived request. A tool might start a CI pipeline, deploy
infrastructure, process a large dataset, wait for an external service, or pause
for human approval. These operations can take minutes or hours, while clients,
proxies, and transports commonly impose much shorter timeouts.

Keeping the original request open for the entire operation is therefore a poor
fit. It consumes a connection, provides limited recovery after a disconnect or
client restart, and makes progress, cancellation, and intermediate user input
difficult to represent. Long-running MCP tools need a protocol-level way to:

- return control to the client quickly;
- identify and recover an operation after reconnecting;
- expose progress and terminal outcomes;
- request more information without relying on an open connection; and
- support cancellation when the underlying operation permits it.

Two related MCP mechanisms address these needs: the MCP Tasks extension and the
Multi Round-Trip Requests (MRTR) pattern.

## MCP Tasks

The [MCP Tasks extension][mcp-tasks] adds asynchronous task execution to MCP.
Instead of blocking until a long-running request completes, a server can return
a durable task handle. The client then observes and interacts with that task
independently of the connection that created it.

A typical task flow is:

1. The client and server advertise support for the Tasks extension.
2. The client sends a supported request, such as `tools/call`.
3. The server durably creates the operation and returns a `CreateTaskResult`
	containing a `taskId`, status, time-to-live, and suggested polling interval.
4. The client calls `tasks/get` until the task reaches a terminal state. A
	server may also publish task notifications when both sides support them.
5. The completed task contains the result that the original request would have
	returned. Failed tasks contain a JSON-RPC error.

The task lifecycle makes long-running work explicit:

| Status | Meaning |
| --- | --- |
| `working` | The operation is still running. |
| `input_required` | The operation is paused until the client supplies input. |
| `completed` | The operation finished and its result is available. |
| `failed` | The operation terminated with an error. |
| `cancelled` | The operation was cancelled. |

Clients can request cancellation with `tasks/cancel`, although cancellation is
cooperative and the server may be unable to stop the underlying work. Clients
should persist task IDs, respect the server's polling interval, and be prepared
to receive either an immediate result or a task result. Servers must create the
task durably before returning its ID and must not return tasks to clients that
did not declare support for the extension.

Tasks are especially useful for queued jobs, external APIs that already expose
job IDs, batch operations, unreliable networks, and workflows with approval or
review gates.

## Multi Round-Trip Requests (MRTR)

The [MRTR pattern][mrtr-pattern] standardizes how an MCP server asks the client
for additional information while processing a request. It replaces a single
long exchange with a sequence of independent request-response round trips:

1. The client sends the initial request.
2. If the server needs more information, it returns an
	`InputRequiredResult`. This may contain `inputRequests`, an opaque
	`requestState`, or both.
3. The client fulfills the input requests. These can include eliciting data or
	approval from a user, asking the model to generate a response, or listing
	roots.
4. The client retries the original operation with the resulting
	`inputResponses` and echoes `requestState` unchanged.
5. The server either completes the operation or requests another round of
	input.

Each retry is a new JSON-RPC request with a new request ID. The server can encode
the context needed to resume into `requestState`, allowing any server instance
to continue the workflow without stateful load balancing or shared session
storage. Clients must treat this value as opaque. Because it passes through the
client, servers must treat it as attacker-controlled and protect its integrity
when it affects authorization, resource access, or business logic. Binding the
state to the authenticated principal, original request, and a short expiry also
limits replay and cross-request reuse.

MRTR applies when a request cannot proceed without client-side information. It
does not by itself provide background execution, durable job identity, progress
tracking, or polling.

## Using Tasks and MRTR Together

Tasks and MRTR solve different parts of a long-running workflow and can be
combined:

- **Tasks manage time:** they detach execution from the original connection and
  provide a durable handle, status, results, and cancellation.
- **MRTR manages interaction:** it defines how the server pauses for information
  that only the client, user, or model can provide and how the client resumes the
  operation.

For a task-backed operation, the server can move the task to `input_required`
and expose its outstanding input requests. The client discovers them through
`tasks/get`, gathers the required responses, and submits them with
`tasks/update`. The task can then return to `working` and eventually reach a
terminal state. This preserves the MRTR input model while retaining the durable
lifecycle supplied by Tasks.

For shorter operations that only need one or more client interactions, MRTR can
be used without creating a task. For long operations that never require client
input, Tasks can be used without an MRTR exchange.

Both documents referenced below describe draft or extension behavior rather
than universally available core behavior. Implementations should negotiate the
required capabilities and verify support in their target MCP clients and
servers.

## Example Server

This repository includes a Python 3.12 server built with FastMCP 4. The
`delayed_hello` tool returns `Hello` after a configurable delay between 10 ms
and 10 minutes. It is declared with `task=True`, and the server registers
`TasksExtension` to implement `io.modelcontextprotocol/tasks`. Task-aware
clients can run the tool in the background and poll for completion. FastMCP's
in-memory task backend is used for this development example, so pending tasks
do not survive a server restart.

The server uses Streamable HTTP and exposes its MCP endpoint at
`http://localhost:8000/mcp`.

The `adaptive_delay` example uses a public synchronous tool and a hidden
task-enabled twin to implement timeout-based cancellation and restart. See
[AdaptiveTasks design notes and validation results](docs/adaptive-tasks.md)
for its behavior, known version/MRTR regressions, and test commands. Expected
failures and Redis-dependent skips must be reported separately from passes.

## Optional Task Execution

The `flexible_hello` tool lets the caller choose whether to wait for an inline
result or create a background task:

```python
@mcp.tool(task=TaskConfig(mode="optional"))
async def flexible_hello(delay_ms: int = 1_000) -> str:
    await asyncio.sleep(delay_ms / 1_000)
    return "Hello from a flexible task"
```

`TaskConfig(mode="optional")` allows two request styles for the same tool:

| Client request | Server response |
| --- | --- |
| A normal `tools/call` request | Waits for the tool and returns its result inline |
| A task-augmented `tools/call` request | Starts background execution and returns a task handle |

Optional mode does not choose a path based on the delay. The caller must
explicitly request task execution and advertise support for the MCP Tasks
extension. An ordinary call with a 10-minute delay still waits for 10 minutes.
A task-aware call with a 1-second delay still creates a background task.

This differs from `task=True`, which requires task execution, and from this
repository's `adaptive_delay`, which automatically promotes calls that exceed
its grace period. Optional mode is useful when the same operation is commonly
fast but callers may already know that a particular invocation belongs in the
background.

`flexible_hello` accepts `delay_ms` values from `10` through `600000`. For
example, `1000` means 1 second and `600000` means 10 minutes. Both execution
paths return `Hello from a flexible task` when the delay finishes.

### Test in VS Code

1. Run **Tasks: Run Task** from the Command Palette and select
	**MCP: Run server**.
2. Open the Chat view and select **Agent** mode.
3. Open the Chat tools picker and enable the tools from
	`long-running-mcp-tools`.
4. Send this prompt to exercise the normal inline path:

	```text
	Call flexible_hello from long-running-mcp-tools with delay_ms 1000.
	```

5. Send this prompt to request the optional background path:

	```text
	Call flexible_hello from long-running-mcp-tools as a background task with delay_ms 600000.
	```

The first call completes after about 1 second. The second call should return a
task without keeping the tool request open for 10 minutes, then make the final
greeting available when the task completes.

After changing tool definitions, stop and restart **MCP: Run server**, then
refresh the server or tool list in VS Code before testing again. If port 8000 is
already in use, stop the older server task before restarting it.

## Development Container

The recommended development environment is the included VS Code dev container.
It contains Python 3.12 and `uv`, installs the Python, Pylance, Ruff, and mypy
extensions, restores the locked dependencies, and forwards port 8000.

1. Open the repository in VS Code.
2. Run **Dev Containers: Reopen in Container** from the Command Palette.
3. Wait for `uv sync --frozen` to complete.
4. Run the **MCP: Run server** task, or run:

	```bash
	uv run long-running-mcp-tools
	```

VS Code discovers the running server through `.vscode/mcp.json`. The server is
intentionally unauthenticated and bound to `0.0.0.0` for local container
development; add authentication before exposing it beyond a trusted local
environment.

## Quality Checks

Run all checks with the default **Python: Check** build task, or run them
individually:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## References

- [MCP Tasks extension overview][mcp-tasks]
- [MCP Multi Round-Trip Requests (MRTR) pattern][mrtr-pattern]
- [FastMCP documentation][fastmcp]
- [FastMCP background tasks][fastmcp-tasks]

[mcp-tasks]: https://modelcontextprotocol.io/extensions/tasks/overview
[mrtr-pattern]: https://modelcontextprotocol.io/specification/draft/basic/patterns/mrtr
[fastmcp]: https://gofastmcp.com/getting-started/welcome
[fastmcp-tasks]: https://gofastmcp.com/servers/tasks