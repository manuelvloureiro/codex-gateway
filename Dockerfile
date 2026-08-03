# Serves a ChatGPT subscription as a keyless OpenAI-compatible provider.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    CODEX_GATEWAY_HOME=/data \
    CODEX_GATEWAY_PORT=8085

WORKDIR /app

# Dependencies resolve from the lock file and cache independently of the
# source, so editing a handler does not reinstall aiohttp.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY app/ ./app/

RUN useradd --create-home --uid 10004 codexgw \
 && mkdir -p /data && chown -R codexgw:codexgw /data /app
USER codexgw

VOLUME ["/data"]
EXPOSE 8085

# /health is 503 until a credential exists, which is the honest answer: the
# process is up but cannot serve a completion.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8085/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "codex_gateway"]
