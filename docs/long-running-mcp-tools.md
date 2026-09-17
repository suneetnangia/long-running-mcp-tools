# Long-Running MCP Tools: Summary

This document summarizes the problem this repository explores, the approach
implemented so far, an approach left for future work, and the current
ecosystem limitations that affect who benefits from either approach today.

## 1. Problem statement

An MCP tool call is a request/response exchange by default. The client
waits for the result. That works for calls returning in milliseconds, but
not for a tool whose duration varies by input, such as a CI pipeline, a
deployment, or a wait for human approval. Holding the connection open risks
a client or proxy timeout.

We want to promote a tool call to an asynchronous **Task** only when it is
actually needed, based on how long the call turns out to take, rather than
committing every call, or none, to the task path in advance.

Task is the new MCP-native standard for this kind of asynchronous execution,
defined by the [MCP Tasks extension][mcp-tasks] (`io.modelcontextprotocol/tasks`,and implemented for FastMCP servers via [FastMCP background tasks][fastmcp-tasks].
A task is a durable, protocol-level handle. The server returns a `taskId`,
the client polls `tasks/get`, and the task can pause for input
(`input_required`) before reaching a terminal state (`completed`, `failed`,
`cancelled`). See this repository's [README](../README.md) for a fuller
walkthrough of Tasks and the related Multi Round-Trip Requests (MRTR)
pattern.


## 2. Approach 1: the server decides when to promote (AdaptiveTasks)

The approach implemented in this repository is [`AdaptiveTasks`](../src/long_running_mcp_tools/adaptive_tasks.py):
the **server** decides, per call, whether to promote to a Task, based on how
long the call has actually taken so far, not a per-tool declaration made in
advance.

Briefly, the idea:

* `AdaptiveTasks.tool()` registers two tools from one function: a public,
  `task=False` tool that clients call directly, and a hidden `task=True`
  "twin" that can run as a background task.
* The public call races against a `grace_period` timer. If it finishes first,
  the result returns directly, just like an ordinary synchronous call. If the
  grace period elapses first, the in-flight attempt is cancelled and the same
  arguments are handed to the twin, which the client (having already
  negotiated the tasks capability) keeps polling via `tasks/get`.
* A `HideAdaptiveTwins` middleware keeps the twin out of `tools/list` so tool
  discovery only ever advertises the public name.

All of that decision-making, racing the call, cancelling it, promoting to the
twin, is wrapped inside `AdaptiveTasks`. A tool author does not implement any
of it. Writing an adaptive tool only means swapping the usual `@mcp.tool()`
decorator for `@adaptive_tasks.tool()`, as [`adaptive_delay`](../src/long_running_mcp_tools/server.py)
does in this repository. Everything else about the function, its signature,
docstring, and body, stays the same as any other FastMCP tool.

This lets fast calls stay fast, paying no task-creation or polling overhead,
while calls that turn out to run long still get a durable task handle instead
of an open connection that risks timing out.

For the full design rationale, the tradeoffs against alternative approaches,
known regressions, and validation results, see
[AdaptiveTasks design notes](./adaptive-tasks.md).

### Example prompt

With the server running and connected in a Task-aware client such as VS Code
chat, `adaptive_delay` can be exercised directly with a prompt like:

```text
Use the long-running-mcp-tools MCP server and call adaptive_delay with
delay_ms set to 60000 with Task support. Tell me the exact result.
```

A 60 second (`60000` ms) delay exceeds the configured `grace_period`, so the
public call is cancelled partway through and the twin tool takes over as a
background task. The client keeps polling `tasks/get` until the task
completes, then returns the same result the synchronous call would have
produced, just delivered through the tasked path instead of an open
connection.

## 3. Approach 2: the client decides when to promote

[TODO: update details]

## 4. Current limitation: client support for Task

The Tasks extension is recent, and client support is uneven.

VS Code (GitHub Copilot Chat) supports Tasks, implemented in
[microsoft/vscode#277888][vscode-tasks-pr] and refined in
[microsoft/vscode#286447][vscode-tasks-update-pr]. It only polls `tasks/get`
when the server declares the tool tasked (`task=True`, or a `TaskConfig` mode
that resolves to tasked execution); a `task=False` tool runs synchronously as
usual. VS Code shows no distinct "running as a background task" indicator, so
both cases still just read as waiting for the answer to the user.

Claude (Desktop/Code) and ChatGPT/Codex do not appear to support the Tasks
extension yet. A search of the [anthropics/claude-code][claude-code-issues]
and [openai/codex][codex-issues] issue trackers found no open tracking issue
for `io.modelcontextprotocol/tasks` support.

> [!NOTE]
> TK: no confirmed public roadmap or release date was found for Tasks support
> in either client. If a tracking issue or announcement turns up, it should
> be linked here.

Because of this, `AdaptiveTasks`' fallback behavior matters in practice
today: `intercept_tool_call` checks whether the calling client declared the
tasks capability, and only enforces the grace period when it did. Clients
without Tasks support get the call run inline to completion, exactly as
today, with no unparseable `CreateTaskResult` returned to them.

## 5. Conclusion

Task is becoming the standard, protocol-native way to represent long-running
MCP tool calls, replacing ad hoc per-server polling schemes with a shared
lifecycle (`working` / `input_required` / `completed` / `failed` /
`cancelled`). The question this repository investigates is *when* to use it:
promoting every call to a Task wastes round trips on fast calls, while never
promoting risks timeouts on slow ones. 

`AdaptiveTasks` answers that question
on the server, from measured elapsed time, with a documented set of tradeoffs
and known regressions (see [AdaptiveTasks design notes](./adaptive-tasks.md)).

[TODO: update conclusion on the approach 2]

In the meantime, client
support for Tasks is still emerging. VS Code has it, while other major AI
clients do not yet appear to, so any adaptive-promotion design needs a
graceful, synchronous fallback for clients that haven't opted in.

## 6. References

* [mcp-tasks]: https://modelcontextprotocol.io/extensions/tasks/overview
* [fastmcp-tasks]: https://gofastmcp.com/servers/tasks
* [vscode-tasks-pr]: https://github.com/microsoft/vscode/pull/277888
* [vscode-tasks-update-pr]: https://github.com/microsoft/vscode/pull/286447
* [claude-code-issues]: https://github.com/anthropics/claude-code/issues
* [codex-issues]: https://github.com/openai/codex/issues
