# Build stage: resolve and install dependencies with uv into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: slim image, non-root user, only the venv, code, rules and sample files.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 uw
WORKDIR /app
COPY --from=build --chown=uw:uw /app /app
COPY --chown=uw:uw rules ./rules
COPY --chown=uw:uw data ./data
RUN mkdir -p /app/var && chown uw:uw /app/var
USER uw
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8782 UW_DB=/app/var/decisions.db UW_LLM_PROVIDER=mock
EXPOSE 8782
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8782/healthz', timeout=2)"
CMD ["uwengine", "serve"]
