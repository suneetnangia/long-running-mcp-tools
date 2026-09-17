---
title: Optional Tasks Design Notes
description: Client-selected inline and background execution using FastMCP optional tasks
---

## The problem it solves

A tool can serve both short interactive requests and longer background work.
Always running inline risks timeouts for slow inputs, while always creating a
Task adds task creation and polling overhead to fast calls.

Sometimes the caller already knows which execution mode suits an operation.
A user may want to wait for a quick check but submit a longer deployment in the
background. Optional task execution lets the client make that choice for each
invocation, without requiring separate foreground and background tools.

The `flexible_hello` example demonstrates this approach. Its implementation
is not yet present on this documentation branch.

## The idea

The server registers one async tool using FastMCP's task configuration:

```python
@mcp.tool(task=TaskConfig(mode="optional", poll_interval=timedelta(seconds=2)))
```

Two request paths follow:

* A call without compatible Task opt-in waits for the tool and returns its
  result inline.
* A call with compatible Task opt-in starts background execution and returns a
  task handle. The client uses the task ID to track the operation and retrieve
  its eventual result.

A FastMCP client's ordinary `call_tool()` can negotiate Task opt-in and poll
transparently under the hood, so an unremarkable-looking call is not
necessarily the inline path; the client's own mode setting decides which
protocol path an outwardly ordinary call takes.

`flexible_hello` accepts `delay_ms` from `10` through `600000`, with a default
of `1000`. Both paths return `Hello from a flexible task` after the delay.

Optional mode does not measure elapsed time or automatically promote a slow
call. An ordinary 10-minute call still waits inline; a task-augmented one-second
call still creates a Task. The client selects the mode when submitting the
request, before the tool body starts.

### Execution modes

FastMCP's `TaskConfig` supports three modes:

* `forbidden` runs calls inline, even when the client supports Tasks. Use it
  when a tool must not enter the background task path.
* `optional` runs inline without Task opt-in and as a background Task with
  compatible opt-in. Use it when either path is acceptable, as with
  `flexible_hello`.
* `required` runs as a background Task with compatible opt-in and returns a
  missing-required-capability error without it. Use it when silently falling
  back to foreground execution is unacceptable.

See the [FastMCP execution modes documentation][fastmcp-execution-modes] for
the framework contract.

## Why it works

The server advertises support for both execution modes through the same tool
registration. FastMCP handles the selected execution path, so the tool author
does not need a hidden twin, a grace-period timer, or a cancellation-and-restart
wrapper.

Background execution requires compatible Task support in the client and the
appropriate request augmentation. The augmentation is protocol metadata, not a
`flexible_hello` argument. A natural-language request to run in the background
only works when the client can translate that intent into a tasked request.
Clients making ordinary calls can receive the inline greeting without handling
a task result.

Choosing the mode before execution avoids the deliberate restart used by
[`AdaptiveTasks`](./adaptive-tasks.md). It does not establish an exactly-once
execution guarantee: retries or worker redelivery can still repeat effects.
Side-effecting operations need their own idempotency or recovery strategy.

The configured two-second polling interval is a hint returned in Task metadata.
The client implements polling and may use a different cadence. This setting
does not enable Task support, change the requested delay, impose a timeout, or
select an execution mode. It has no effect on foreground calls.

## Comparing it to the alternatives

| Option                          | How it decides                         | Main weakness                                      |
|---------------------------------|----------------------------------------|----------------------------------------------------|
| Always inline (`task=False`)     | Every call waits for the result        | Slow calls remain exposed to request timeouts       |
| Task-required execution         | Every supported call becomes a Task    | Fast calls pay task creation and polling overhead   |
| Input-based duration prediction | Estimates runtime before starting      | Estimates can be wrong when external state changes  |
| `AdaptiveTasks`                  | Promotes after a server grace period   | Cancellation and restart can repeat completed work  |
| Optional tasks                  | Client chooses when submitting a call  | Requires client support and an advance mode decision |

## Pros

* One public tool supports both execution modes with the same arguments and
  result.
* Callers can use knowledge of the operation and user intent to choose the
  execution mode.
* Foreground calls avoid task creation and polling overhead.
* Background calls avoid holding the initial request open for the entire
  operation.
* No adaptive cancellation, hidden twin, or restart is needed to select the
  background path.
* Ordinary calls remain usable by clients without compatible Task support.

## Cons

* Optional mode does not rescue a slow foreground call after it starts.
* Clients must implement compatible Task negotiation, submission, and result
  handling to use the background path.
* Different clients may apply different policies when choosing execution mode.
* A Task on the server does not guarantee a nonblocking chat interface or a
  visible task ID. Those are client UI decisions.
* Persistence and recovery depend on the task backend, not on optional mode.

## Regression checks and known issues

The `flexible_hello` example includes four checks in `tests/test_server.py`:

* An ordinary client call returns the expected greeting.
* A call submitted through `call_tool_task` returns the expected greeting after
  awaiting the task.
* The tool configuration is `optional` with a two-second polling interval.
* The advertised input schema includes the minimum and maximum delay bounds.

These checks cover the example's result, configuration, and schema. A greeting
alone does not prove inline execution: a client library can submit a Task and
wait for it internally. Inspect the request and response exchange when
verifying mode selection in a particular client.

On a checkout containing `flexible_hello` and its tests, run the focused checks
with:

```bash
uv run pytest tests/test_server.py -k flexible_hello -v
```

This documentation update does not run those tests or claim a new passing
result. These four checks do not establish support in every chat client.

## Lifecycle and failure validation

The example server uses an in-memory task backend. Pending tasks do not survive
a server restart merely because they were submitted in optional mode.

The lifecycle and failure results in the
[AdaptiveTasks design notes](./adaptive-tasks.md) concern separate test
scenarios. They should not be presented as optional-tool crash recovery, MRTR,
or exactly-once validation.

### Test in VS Code

Use a checkout containing `flexible_hello`. Start **MCP: Run server**, connect the MCP
server in VS Code, and enable `flexible_hello` in the Agent chat tools picker.
Restart the server and refresh tool discovery after changing tool definitions.

Request a short call with:

```text
Call flexible_hello from long-running-mcp-tools with delay_ms 1000.
```

Request background execution with:

```text
Call flexible_hello from long-running-mcp-tools as a background task with
delay_ms 600000.
```

The intended paths are an inline greeting after about one second and a Task
submission for the 10-minute operation. Prompt wording alone does not guarantee
that the client selects either mode.

For evidence of background execution, inspect the MCP exchange for an early
response containing a task ID, followed by task status checks. Waiting text in
chat, progress notifications, or the final greeting alone do not distinguish
the paths. Check capability negotiation and request augmentation if the call
waits inline despite a background request.

### Reporting results

Record the client and server versions, negotiated Task support, submitted
execution mode, whether a task ID was returned, and the final outcome. Report
the advertised polling hint separately from the observed polling cadence.
Distinguish protocol evidence from chat UI behavior and automated Python tests.

## Review decisions

Before adopting optional execution for a production tool, agree on:

1. Which client policy or user action selects background execution.
2. Whether foreground execution is acceptable when Task support is unavailable.
3. Which persistence, retry, and idempotency guarantees the operation requires.
4. How the client presents task status, cancellation, and the final result.

Optional tasks fit operations where the caller can choose in advance.
`AdaptiveTasks` instead decides from elapsed time, with the cancellation and
restart tradeoffs documented separately. Neither configuration supplies client
support or business-level recovery guarantees by itself.

[fastmcp-execution-modes]: https://gofastmcp.com/servers/tasks#execution-modes