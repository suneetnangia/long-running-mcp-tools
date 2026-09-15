# Project Instructions

- Use Python 3.12 and manage dependencies with `uv`.
- Keep application code under `src/long_running_mcp_tools` and tests under `tests`.
- Use async functions for task-enabled FastMCP tools.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
  `uv run pytest` before completing changes.
- Follow the [FastMCP documentation](https://gofastmcp.com/getting-started/welcome)
  and [FastMCP background tasks documentation](https://gofastmcp.com/servers/tasks).
- The server uses Streamable HTTP at `http://localhost:8000/mcp`.
