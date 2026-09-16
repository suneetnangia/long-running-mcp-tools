# AdaptiveTasks Design Notes

## The problem it solves

A FastMCP tool is normally declared as either always synchronous or always a
background task (`task=True`). Neither extreme fits a tool whose duration
varies by input, such as [`adaptive_delay`](../src/long_running_mcp_tools/server.py) in this repository: a 50 ms call and a
five-minute call go through the same code path. Marking it `task=True`
forces every call, even the fast ones, through task creation and client
polling. Leaving it synchronous risks the request hanging for the slowest
inputs.

[`AdaptiveTasks`](../src/long_running_mcp_tools/adaptive_tasks.py) removes that
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
normal discovery only advertises the public name. The twin remains callable
by name and resolvable for `create_task` and `tasks/get`; hiding it from
discovery is not an access-control boundary.

## Why it works

Two details support the basic execution flow, subject to the limitations below.

**Cancellation before restart, not exactly-once execution.** When the grace period elapses, the
original attempt is cancelled and awaited (with `CancelledError` suppressed)
before the twin starts. This avoids deliberately abandoning the old coroutine
to run alongside its replacement, but the replacement starts from the
beginning. Completed work can be repeated, and cancelling a coroutine does
not undo external effects or necessarily stop threads/remote operations.
Side-effecting tools still need idempotency or durable checkpoints; this
pattern alone guarantees neither no lost work nor no duplicated effects.

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
* Compatible fallback: a client that can't accept promotion gets its call run
  inline, instead of an unparseable response. This is not a fail-closed policy
  or a bounded response-time guarantee.
* Decides from real elapsed time, not a guess from the input, so it stays
  adaptive when duration depends on external state. That decision does not
  by itself establish safe restart behavior.
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

## Regression checks and known issues

The validation branch keeps the twin-based implementation unchanged.
[`test_adaptive_tasks_regressions.py`](../tests/test_adaptive_tasks_regressions.py)
adds two real FastMCP client/server checks, including a passing foreground
control in each test:

| ID | Intended behavior | Reproduced behavior |
| --- | --- | --- |
| REG-1 | A call requesting version 1.0 still executes that version after promotion. | The inline control returns `v1`; after registering versions 1.0 and 2.0, the promoted call returns `v2`. The twin has no version and the mapping is keyed only by public name. |
| REG-2 | An already-answered foreground MRTR continuation must not silently lose its input/state and ask again after promotion. | The foreground control asks once. A slow approved continuation is cancelled, restarted without responses/state, and asks the user a second time. |

Both are **known unresolved failures**, marked with `xfail(strict=True)` and
distinct assertion exception types. Only the precisely reproduced symptom is
expected to fail; setup, transport, unrelated assertions, or timeout failures
remain ordinary failures. A future unexpected pass fails the suite until its
marker is removed. To expose the failures without expected-failure handling:

```bash
uv run pytest tests/test_adaptive_tasks_regressions.py --runxfail -v --tb=short
```

On 2026-09-16 with FastMCP 4.0.3 this command produced **two failures**; the
normal command reports **two xfailed**, not two passed:

```bash
uv run pytest tests/test_adaptive_tasks_regressions.py -v -rx
```

REG-2 can be addressed by preserving continuation state or explicitly rejecting
unsupported continuation promotion. Repeated approval is not a substitute for
either policy. These tests do not choose a replacement architecture.

## Lifecycle and failure validation

The previous experiment suite has been adapted to this branch without copying
its single-tool implementation or modifying application code. Test-only servers
register the colleague's `AdaptiveTasks` decorator, standard `TasksExtension`,
and `HideAdaptiveTwins`; isolated CLI subprocesses exercise the HTTP boundary.

On 2026-09-16 with FastMCP 4.0.3, pydocket 0.25.2, and Redis 7.0.15, all seven
migrated scenarios passed:

| Scenario | Observed result |
| --- | --- |
| Client exits; a new process resumes using the task ID | The adaptive task remains available, prompts for approval, and completes or declines according to the scripted stdin answer. |
| Worker concurrency is 1 while approval is pending | Another genuinely queued task completes while the first remains `input_required`. |
| Approval stdin closes without an answer | No automatic approval; the task stays pending and a new client can cancel it. |
| Unknown task ID | Explicit failure, not resubmission or a fabricated result. |
| Server A stops; server B connects to the same Redis | Original task identity and outstanding input survive; the resumed result can be retrieved. |
| Server A is killed after four durable step records | B redelivers the original task and starts from step 1, yielding `[1,2,3,4,1,2,3,4,5,6,7,8,9,10]`. |
| Two servers receive identical approvals concurrently, then receive the same answers again after completion | Four `tasks/update` requests produce one approved-continuation effect in the observed run. |

The crash probe deliberately uses a task-required tool to separate Docket
redelivery from adaptive foreground cancellation. Its test-only redelivery
timeout is 2 seconds (not the production default of 300 seconds). One run
recorded the replacement invocation starting about 2.33 seconds after the old
process exited. This is a local measurement, not a response-time guarantee.
The ten-step job produced **14 effects**, demonstrating replay rather than
checkpoint recovery or exactly-once execution.

Passing a task-backed input flow does not resolve REG-2: that test specifically
starts the MRTR exchange in the foreground and then promotes its slow
continuation. The two paths must not be conflated.

Run the suite with a dedicated, already-running Redis/Valkey instance:

```bash
MCP_TEST_REDIS_URL=redis://localhost:6379/0 \
  uv run pytest tests/test_lifecycle.py tests/test_failure_recovery.py -v -s
```

Without `MCP_TEST_REDIS_URL`, the four memory-backed lifecycle cases run and the
three Redis-dependent cases are explicitly skipped. Tests launch their own
server/client processes on isolated ports and use unique Redis namespaces;
they do not modify a server already listening on port 8000 or flush a shared
Redis database. Scripted yes/no input is for repeatable simulated approval,
not evidence of a real production authorization decision.

These results apply to separate processes on one host with Redis remaining
alive. They do not establish cross-device networking, Redis restart durability,
network-partition behavior, authenticated tenant isolation, large-scale load
handling, or exactly-once business effects under every race/crash boundary.

### Reporting results

With Redis enabled, the complete validation branch contains **25 passed and
2 xfailed**, with no skipped tests. The 18 original tests remain passing.
The two expected failures are unresolved regressions, not successful checks.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
MCP_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -ra
```

## Review decisions

This branch adds evidence rather than changing the execution implementation.
Before merging fixes, agree on:

1. Whether the decorator supports versioned tools and which registration
   settings must be preserved by the background twin.
2. Whether a foreground MRTR continuation can be promoted with its answers
   intact; if unsupported, how the limitation should be reported to the caller.
3. The restart-safety contract for side-effecting tools. Waiting for cancellation
   does not make a repeated database write or external deployment idempotent.

A previously tested alternative subclasses `TasksExtension` and routes one
registered tool either inline or into Docket. It avoids twin identity/configuration
drift, but still depends on the internal task-creation seam and has the same
cancel-and-restart tradeoff. It is an option for discussion, not a replacement
made by this validation branch. The decorator interface could be kept with
either implementation.
