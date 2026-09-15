FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

RUN pip install --no-cache-dir uv==0.12.14

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/workspaces/long-running-mcp-tools/.venv
