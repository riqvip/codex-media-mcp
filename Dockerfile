FROM python:3.13-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Use the same locked dependencies as local development.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIA_MCP_HOST=0.0.0.0 \
    MEDIA_MCP_PORT=8765 \
    MEDIA_HOST_FS_ROOT=/host
EXPOSE 8765
CMD ["python", "-m", "codex_media_mcp.server"]

FROM runtime AS test
RUN uv sync --frozen --no-install-project
COPY tests ./tests
RUN python -m pytest -q

FROM runtime AS final
