# AdaptiveTasks Design Notes

## The problem it solves

A FastMCP tool is normally declared as either always synchronous or always a
background task (`task=True`). Neither extreme fits a tool whose duration
varies by input, such as [`adaptive_delay`](/workspaces/long-running-mcp-tools/src/long_running_mcp_tools/server.py) in this repository: a 50 ms call and a
five-minute call go through the same code path. Marking it `task=True`
forces every call, even the fast ones, through task creation and client
polling. Leaving it synchronous risks the request hanging for the slowest
inputs.

[`AdaptiveTasks`](/workspaces/long-running-mcp-tools/src/long_running_mcp_tools/adaptive_tasks.py) removes that
tradeoff: it decides per call, based on measured elapsed time, rather than
per tool declaration.

## The idea

`AdaptiveTasks.tool()` registers two tools from a single function:

* A public tool, `task=False`, that clients call directly.
* A hidden "twin" named `<tool>__adaptive_task`, `task=True`, that Docket can
  run as a background task.

`AdaptiveTasks.intercept_tool_call` races the public tool's real invocation
against a `grace_period` timer using `asyncio.wait_for`. Two outcomes follow:

* The call finishes inside the grace period: its result (or exception)
  returns directly, exactly as a normal synchronous tool call would.
* The call is still running when the grace period elapses: the in-flight
  attempt is cancelled, and `create_task` hands the same arguments to the
  twin. The client, which already asked for the tasks capability, keeps
  polling `tasks/get` for the eventual result instead of the request timing
  out.

`HideAdaptiveTwins` middleware filters twin tools out of `tools/list` so
clients only ever see and call the public name; the twin still resolves by
name for `create_task` and for `tasks/get` to look up later.

## Why it works

Two details make the design correct rather than merely clever.

**No dropped or duplicated work.** When the grace period elapses, the
original attempt is cancelled and awaited (with `CancelledError` suppressed)
before the twin starts, so exactly one execution of the tool body is ever
in flight. A naive implementation that merely abandoned the original
coroutine could leave two concurrent executions of a side-effecting tool
running at once; this code explicitly guards against that instead.

**Respects what the client can actually parse.** Promotion sends back a
`CreateTaskResult` in place of the tool's real result. A client that never
declared the tasks capability cannot parse that shape, which fails response
validation client-side rather than degrading gracefully. `intercept_tool_call`
checks `context.client_extension_settings(TASKS_EXTENSION_ID)` up front and,
if the client has not opted in, lets the call run to completion with no
grace period enforced at all. The feature only activates for clients that
can handle it.

The two-tool structure exists because a single `task=True` tool is already
fully owned by FastMCP's `TasksExtension`: once a client has negotiated the
tasks capability (every `fastmcp` client does automatically), the
`optional`/`required` task modes promote a `task=True` call before its body
ever runs, leaving no grace period to race. Keeping the public tool
`task=False` guarantees `TasksExtension` never intercepts it first, so
`AdaptiveTasks` remains the sole decision-maker for whether a given call is
promoted.

## Comparing it to the alternatives

A tool author who needs to handle variable-duration work has a few viable
options besides racing a grace period. Each one makes a different tradeoff
between latency, implementation effort, and correctness risk.

| Option                                                     | How it decides                                                          | Main weakness                                                                                     |
|--------------------------------------------------------------|--------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------|
| Always synchronous (`task=False`)                           | Never promotes; the request just waits.                                  | Slow inputs hold the connection open for as long as the tool runs, up to the client's own timeout.  |
| Always a task (`task=True`), as `delayed_hello` does in this repo | Every call is promoted before its body runs.                            | Fast calls pay for task creation and `tasks/get` polling round trips they never needed.               |
| A fixed duration heuristic based on input (for example, sleep tools estimating from `delay_ms`) | Promotes based on a prediction made before the call starts.              | Only works when duration is predictable from the arguments; wrong for anything with variable I/O, retries, or external dependencies. |
| Client-side timeout, wrapping any tool in a race on the client | The client, not the server, decides when to give up and poll separately. | Every client integrating the tool must reimplement the same race and polling logic themselves.        |
| `AdaptiveTasks` (this branch)                                | Races the real call against a server-side `grace_period`; promotes only if still running. | A single shared `grace_period` is a guess, and it costs one cancelled attempt when a call straddles it. |

## Pros

* Fast calls stay fast: only the calls that actually run long pay for task
  creation and polling, unlike `delayed_hello`'s always-a-task approach.
* One server-side decision instead of per-client logic: no client needs to
  reimplement a race-and-poll wrapper of its own.
* Fails closed: a client that can't accept promotion just gets its call run
  inline, instead of an unparseable response.
* Decides from real elapsed time, not a guess from the input, so it stays
  correct even when duration depends on external state.
* Adds no new framework machinery: it is a `ServerExtension` plus a
  `Middleware`, layered on FastMCP's existing task support.

## Cons

* `grace_period` is one shared guess. A tool whose duration straddles it
  regularly pays for both a cancelled attempt and a promotion.
* A promoted call restarts from scratch rather than resuming, so tools with
  non-idempotent side effects early in their body need extra care.
* Adds moving parts: a hidden twin tool per registered tool, plus a
  dependency on `HideAdaptiveTwins`, `TasksExtension`, and a working Docket
  backend.
