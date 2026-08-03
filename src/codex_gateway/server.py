"""HTTP surface: an OpenAI-compatible front end over the ChatGPT Codex backend.

Two groups of routes: the provider surface (``/responses``,
``/chat/completions``, ``/models``), and ``/auth/*``, which drives the
device-code login over HTTP so the service can be signed in without a shell.

``CODEX_ADMIN_TOKEN`` optionally gates ``/auth/*``. It protects credential
management, not use: the provider surface must stay open for keyless clients,
so whoever reaches the port can spend the subscription either way.

``oauth`` is synchronous, so every call into it goes through
``asyncio.to_thread`` — a blocking refresh would stall in-flight streams.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from . import oauth, translate
from .oauth import AuthError

log = logging.getLogger("codex_gateway.server")

UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=900)
MAX_BODY_BYTES = 64 * 1024 * 1024

SESSION_KEY: web.AppKey[Any] = web.AppKey("session")
PENDING_LOGINS_KEY: web.AppKey[dict] = web.AppKey("pending_logins")

# How many device logins may be in flight at once. Each is a few hundred bytes;
# the cap only exists so an unauthenticated caller cannot grow the dict forever.
MAX_PENDING_LOGINS = 8


def _error(message: str, status: int, code: str | None = None) -> web.Response:
    payload: dict[str, Any] = {"error": {"message": message}}
    if code:
        payload["error"]["code"] = code
    return web.json_response(payload, status=status)


def _auth_error_response(exc: AuthError) -> web.Response:
    # 429 is upstream quota, not a credential problem — passing it through as
    # 401 would send clients into a pointless re-login loop.
    status = 429 if exc.code == "rate_limited" else 401
    return _error(str(exc), status, exc.code)


async def _read_json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPBadRequest(text='{"error": {"message": "invalid JSON body"}}',
                                 content_type="application/json") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"error": {"message": "body must be a JSON object"}}',
                                 content_type="application/json")
    return body


async def _resolve(force_refresh: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(oauth.resolve_credentials, force_refresh=force_refresh)


# --------------------------------------------------------------------------
# Admin guard
# --------------------------------------------------------------------------

def _admin_token() -> str:
    return (os.getenv("CODEX_ADMIN_TOKEN") or "").strip()


def require_admin(handler):
    """Gate ``/auth/*`` behind a bearer token when CODEX_ADMIN_TOKEN is set.

    Unset means open, which is the right default for a loopback-only container.
    Set it when the port is reachable by anyone you would not hand your ChatGPT
    login to — these routes sign the gateway in and out.

    This guards credential management only. ``/chat/completions`` stays open by
    necessity, so it is not a defence against someone spending the subscription.
    """
    async def wrapped(request: web.Request) -> web.StreamResponse:
        expected = _admin_token()
        if expected:
            header = request.headers.get("Authorization", "")
            provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
            # Constant-time: this compares a secret against attacker-supplied input.
            if not provided or not secrets.compare_digest(provided, expected):
                return _error("admin token required", 403, "admin_token_required")
        return await handler(request)

    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    return wrapped


# --------------------------------------------------------------------------
# Provider surface
# --------------------------------------------------------------------------

async def handle_responses(request: web.Request) -> web.StreamResponse:
    """Proxy /responses straight through, refreshing once on a 401."""
    body = await _read_json(request)

    try:
        creds = await _resolve()
    except AuthError as exc:
        return _auth_error_response(exc)

    session: aiohttp.ClientSession = request.app[SESSION_KEY]
    payload = translate.coerce_body(body)

    upstream = await _post_upstream(session, creds, payload)

    # A 401 usually means another client (Codex CLI, a second gateway) rotated
    # the refresh token out from under us: our access token looks fine locally
    # but is dead on the wire. Force a refresh and retry exactly once.
    if upstream.status == 401:
        upstream.release()
        log.info("upstream 401 — forcing a token refresh and retrying once")
        try:
            creds = await _resolve(force_refresh=True)
        except AuthError as exc:
            return _auth_error_response(exc)
        upstream = await _post_upstream(session, creds, payload)

    out = web.StreamResponse(
        status=upstream.status,
        headers={"Content-Type": upstream.headers.get("Content-Type", "text/event-stream")},
    )
    await out.prepare(request)
    try:
        async for chunk in upstream.content.iter_any():
            await out.write(chunk)
    finally:
        upstream.release()
    await out.write_eof()
    return out


async def _post_upstream(session: aiohttp.ClientSession, creds: dict[str, Any],
                         payload: dict[str, Any]):
    return await session.post(
        f"{creds['base_url']}/responses",
        json=payload,
        headers=translate.codex_headers(creds["api_key"], creds.get("account_id")),
    )


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """Serve /chat/completions by translating to and from the Responses API.

    Bifrost forwards the request type it was given — it does not convert
    chat_completion into responses. Without this, only Responses-native clients
    can use the subscription; with it, anything speaking OpenAI chat
    completions works.
    """
    body = await _read_json(request)
    model = body.get("model", "")
    wants_stream = bool(body.get("stream"))

    tool_names = [
        (t.get("function") or {}).get("name") or t.get("name")
        for t in (body.get("tools") or []) if isinstance(t, dict)
    ]
    log.info("chat/completions: model=%s stream=%s tools=%d %s",
             model, wants_stream, len(tool_names), tool_names[:8])

    try:
        creds = await _resolve()
    except AuthError as exc:
        return _auth_error_response(exc)

    session: aiohttp.ClientSession = request.app[SESSION_KEY]
    upstream = await _post_upstream(session, creds, translate.chat_request_to_responses(body))

    if upstream.status >= 400:
        detail = (await upstream.read())[:400].decode(errors="replace")
        upstream.release()
        return _error(f"upstream {upstream.status}: {detail}", upstream.status)

    created = int(time.time())
    chunk_id = "chatcmpl-codex"

    if not wants_stream:
        return await _collect_chat_completion(upstream, chunk_id=chunk_id,
                                              created=created, model=model)
    return await _stream_chat_completion(request, upstream, chunk_id=chunk_id,
                                         created=created, model=model)


async def _collect_chat_completion(upstream, *, chunk_id: str, created: int,
                                   model: str) -> web.Response:
    aggregator = translate.ResponseAggregator()
    try:
        async for line in upstream.content:
            kind, event = translate.decode_sse_line(line)
            if kind == translate.SSE_DONE:
                break
            if kind == translate.SSE_EVENT:
                aggregator.feed(event)
    finally:
        upstream.release()
    return web.json_response(
        aggregator.to_chat_completion(chunk_id=chunk_id, created=created, model=model)
    )


async def _stream_chat_completion(request: web.Request, upstream, *, chunk_id: str,
                                  created: int, model: str) -> web.StreamResponse:
    translator = translate.ChatStreamTranslator()
    out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await out.prepare(request)

    def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
        return translate.sse_chunk(chunk_id=chunk_id, created=created, model=model,
                                   delta=delta, finish_reason=finish)

    await out.write(frame({"role": "assistant"}))
    try:
        async for line in upstream.content:
            kind, event = translate.decode_sse_line(line)
            if kind == translate.SSE_DONE:
                break
            if kind != translate.SSE_EVENT:
                continue
            for delta in translator.feed(event):
                await out.write(frame(delta))
    finally:
        upstream.release()

    await out.write(frame({}, translator.finish_reason))
    await out.write(b"data: [DONE]\n\n")
    await out.write_eof()
    return out


async def handle_models(_request: web.Request) -> web.Response:
    """The backend requires a client_version param and then returns an empty
    list, so we serve a static catalogue. Bifrost only needs ids to route."""
    models = [m.strip() for m in
              os.getenv("CODEX_MODELS", "gpt-5.6-sol,gpt-5.4").split(",") if m.strip()]
    return web.json_response({
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "openai"} for m in models],
    })


async def handle_health(_request: web.Request) -> web.Response:
    """Liveness plus credential state.

    Unauthenticated is a 503: the container is running but cannot serve a
    completion, and a load balancer should know that.
    """
    status = await asyncio.to_thread(oauth.credential_status)
    if not status.get("authenticated"):
        return web.json_response({"status": "unauthenticated", **status}, status=503)
    return web.json_response({"status": "ok", **status})


# --------------------------------------------------------------------------
# Login API
# --------------------------------------------------------------------------

@require_admin
async def handle_auth_status(_request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(oauth.credential_status))


@require_admin
async def handle_login_start(request: web.Request) -> web.Response:
    """Begin a device-code login.

    Returns the URL and code to enter. Nothing is stored until the user
    approves and the client calls /auth/login/poll.
    """
    pending: dict[str, oauth.DeviceAuth] = request.app[PENDING_LOGINS_KEY]
    _prune_pending(pending)
    if len(pending) >= MAX_PENDING_LOGINS:
        return _error("too many device logins in flight; wait for one to expire",
                      429, "too_many_pending_logins")

    try:
        auth = await asyncio.to_thread(oauth.start_device_login)
    except AuthError as exc:
        return _auth_error_response(exc)

    pending[auth.device_auth_id] = auth
    log.info("device login started (code %s)", auth.user_code)
    return web.json_response({"status": "pending", **auth.public()})


@require_admin
async def handle_login_poll(request: web.Request) -> web.Response:
    """Check whether the user approved the code; save tokens when they have.

    ``status`` is ``pending`` until approval and ``complete`` once — clients
    poll this at the ``interval`` returned by /auth/login/start.
    """
    body = await _read_json(request)
    device_auth_id = str(body.get("device_auth_id") or "").strip()
    pending: dict[str, oauth.DeviceAuth] = request.app[PENDING_LOGINS_KEY]
    _prune_pending(pending)

    auth = pending.get(device_auth_id)
    if auth is None:
        return _error("unknown or expired device_auth_id; start a new login",
                      404, "unknown_device_auth_id")

    try:
        tokens = await asyncio.to_thread(oauth.poll_device_login, auth)
    except AuthError as exc:
        pending.pop(device_auth_id, None)
        return _auth_error_response(exc)

    if tokens is None:
        return web.json_response({"status": "pending", **auth.public()})

    pending.pop(device_auth_id, None)
    await asyncio.to_thread(oauth.save_tokens, tokens, label="device-code-login")
    log.info("device login complete — credentials saved")
    return web.json_response({
        "status": "complete",
        **await asyncio.to_thread(oauth.credential_status),
    })


@require_admin
async def handle_auth_import(_request: web.Request) -> web.Response:
    """Adopt the local Codex CLI's tokens instead of running a new login."""
    tokens = await asyncio.to_thread(oauth.import_codex_cli_tokens)
    if not tokens:
        return _error(
            f"no usable tokens in the Codex CLI store ({oauth.codex_cli_auth_path()})",
            404, "codex_cli_tokens_unavailable",
        )
    await asyncio.to_thread(oauth.save_tokens, tokens, label="imported-from-codex-cli")
    return web.json_response({
        "status": "imported",
        **await asyncio.to_thread(oauth.credential_status),
    })


@require_admin
async def handle_auth_refresh(_request: web.Request) -> web.Response:
    try:
        await _resolve(force_refresh=True)
    except AuthError as exc:
        return _auth_error_response(exc)
    return web.json_response({
        "status": "refreshed",
        **await asyncio.to_thread(oauth.credential_status),
    })


@require_admin
async def handle_auth_logout(_request: web.Request) -> web.Response:
    cleared = await asyncio.to_thread(oauth.clear_tokens)
    return web.json_response({"status": "cleared" if cleared else "nothing_to_clear"})


def _prune_pending(pending: dict[str, oauth.DeviceAuth]) -> None:
    for key in [k for k, v in pending.items() if v.expired()]:
        pending.pop(key, None)


# --------------------------------------------------------------------------
# Reference UI
# --------------------------------------------------------------------------

# The example app lives outside the package, at the service root, because it
# is a reference to copy from rather than something the wheel should ship.
DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "app"


def login_page() -> Path:
    return Path(os.getenv("CODEX_UI_DIR") or DEFAULT_UI_DIR) / "index.html"


async def handle_index(_request: web.Request) -> web.Response:
    """A one-file page that signs in and sends a test completion.

    Reference, not a product: it shows the /auth/* call sequence for whoever
    wires this service into a real front end. Set CODEX_UI=0 to not serve it.
    """
    if os.getenv("CODEX_UI", "1") == "0":
        return _error("reference UI is disabled (CODEX_UI=0)", 404, "ui_disabled")
    try:
        return web.Response(text=login_page().read_text(encoding="utf-8"),
                            content_type="text/html")
    except OSError:
        return _error(f"reference UI not found at {login_page()}", 404, "ui_missing")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

def build_app(session_factory=None) -> web.Application:
    """Build the app.

    ``session_factory`` exists so tests can supply a fake upstream without
    mutating an already-started application.
    """
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[PENDING_LOGINS_KEY] = {}

    # Both bare and /v1-prefixed paths: Bifrost strips the prefix, but other
    # OpenAI clients keep it.
    for prefix in ("", "/v1"):
        app.router.add_post(f"{prefix}/responses", handle_responses)
        app.router.add_post(f"{prefix}/chat/completions", handle_chat_completions)
        app.router.add_get(f"{prefix}/models", handle_models)

    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/auth/status", handle_auth_status)
    app.router.add_post("/auth/login/start", handle_login_start)
    app.router.add_post("/auth/login/poll", handle_login_poll)
    app.router.add_post("/auth/import", handle_auth_import)
    app.router.add_post("/auth/refresh", handle_auth_refresh)
    app.router.add_post("/auth/logout", handle_auth_logout)

    async def _startup(a: web.Application) -> None:
        a[SESSION_KEY] = (session_factory() if session_factory is not None
                          else aiohttp.ClientSession(timeout=UPSTREAM_TIMEOUT))

    async def _cleanup(a: web.Application) -> None:
        await a[SESSION_KEY].close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


def run(port: int | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Serve even when unauthenticated: /auth/* is how you sign in, so exiting
    # here would make the HTTP login flow unreachable. /health reports 503 and
    # completions return 401 until credentials exist.
    status = oauth.credential_status()
    if status.get("authenticated"):
        log.info("ChatGPT account %s, upstream %s",
                 status.get("account_id"), status.get("base_url"))
    else:
        log.warning("no credentials yet (%s) — sign in with `python -m codex_gateway.login` "
                    "or POST /auth/login/start", status.get("code"))
    if not _admin_token():
        log.info("CODEX_ADMIN_TOKEN is unset — /auth/* is open. Fine on loopback; "
                 "set it if this port is reachable by anyone else.")
    web.run_app(build_app(),
                host=os.getenv("CODEX_GATEWAY_HOST", "0.0.0.0"),
                port=port if port is not None
                     else int(os.getenv("CODEX_GATEWAY_PORT", "8085")),
                print=None)
    return 0
