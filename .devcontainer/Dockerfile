FROM ghcr.io/astral-sh/uv:0.12.14 AS uv

FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/workspaces/long-running-mcp-tools/.venv
