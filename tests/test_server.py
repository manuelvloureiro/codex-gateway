"""HTTP surface: the provider routes Bifrost calls and the /auth/* login API.

The upstream Codex backend is replaced by a fake session, so these exercise the
real handlers, routing, and streaming without a network or a subscription.
"""
from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from codex_gateway import oauth, server
from codex_gateway.oauth import AuthError


class FakeContent:
    """Stands in for aiohttp's StreamReader over a fixed list of lines."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class FakeUpstream:
    def __init__(self, status: int = 200, chunks: list[bytes] | None = None,
                 body: bytes = b"", headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.content = FakeContent(chunks or [])
        self._body = body
        self.released = False

    async def read(self) -> bytes:
        return self._body

    def release(self) -> None:
        self.released = True


class FakeSession:
    """Records upstream POSTs and replays queued responses."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queue: list[FakeUpstream] = []

    async def post(self, url, *, json=None, headers=None, **kwargs):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        if not self.queue:
            raise AssertionError(f"no queued upstream response for {url}")
        return self.queue.pop(0)

    async def close(self) -> None:
        pass


@pytest.fixture
async def client():
    """A running app whose upstream session is the fake."""
    fake = FakeSession()
    test_client = TestClient(TestServer(server.build_app(session_factory=lambda: fake)))
    await test_client.start_server()
    test_client.upstream = fake

    yield test_client
    await test_client.close()


@pytest.fixture
def signed_in(fresh_token):
    """Valid credentials in the isolated store."""
    oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
    return fresh_token


def sse(events: list[dict]) -> list[bytes]:
    return [f"data: {json.dumps(e)}\n".encode() for e in events] + [b"data: [DONE]\n"]


# --------------------------------------------------------------------------
# Models / health
# --------------------------------------------------------------------------

class TestModels:
    async def test_serves_a_static_catalogue(self, client):
        resp = await client.get("/models")
        assert resp.status == 200
        body = await resp.json()
        assert body["object"] == "list"
        assert [m["id"] for m in body["data"]] == ["gpt-5.6-sol", "gpt-5.4"]

    async def test_configurable_via_env(self, client, monkeypatch):
        monkeypatch.setenv("CODEX_MODELS", "gpt-a, gpt-b ,")
        body = await (await client.get("/models")).json()
        assert [m["id"] for m in body["data"]] == ["gpt-a", "gpt-b"]

    async def test_v1_prefix_also_routes(self, client):
        assert (await client.get("/v1/models")).status == 200


class TestHealth:
    async def test_503_when_unauthenticated(self, client):
        resp = await client.get("/health")
        assert resp.status == 503
        body = await resp.json()
        assert body["status"] == "unauthenticated"
        assert body["authenticated"] is False

    async def test_ok_when_signed_in(self, client, signed_in):
        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["account_id"] == "acct-123"

    async def test_never_leaks_the_token(self, client, signed_in):
        assert signed_in not in await (await client.get("/health")).text()


# --------------------------------------------------------------------------
# /responses passthrough
# --------------------------------------------------------------------------

class TestResponses:
    async def test_streams_upstream_bytes_through(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=[b"chunk-a", b"chunk-b"]))
        resp = await client.post("/responses", json={"model": "m", "input": "hi"})
        assert resp.status == 200
        assert await resp.read() == b"chunk-achunk-b"

    async def test_applies_backend_invariants(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=[b"x"]))
        await client.post("/responses", json={"model": "m", "input": "hi",
                                              "stream": False, "store": True})
        sent = client.upstream.calls[0]["json"]
        assert sent["stream"] is True
        assert sent["store"] is False
        assert sent["input"] == [{"role": "user", "content": "hi"}]

    async def test_sends_first_party_headers(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=[b"x"]))
        await client.post("/responses", json={"model": "m", "input": "hi"})
        headers = client.upstream.calls[0]["headers"]
        assert headers["Authorization"] == f"Bearer {signed_in}"
        assert headers["originator"] == "codex_cli_rs"
        assert headers["ChatGPT-Account-ID"] == "acct-123"

    async def test_401_triggers_one_forced_refresh_and_retry(self, client, signed_in,
                                                             monkeypatch, stub_post):
        """Another client can rotate the refresh token out from under us."""
        from .conftest import make_jwt, response
        import time as _time

        rotated = make_jwt(exp=_time.time() + 3600, account_id="acct-rotated")
        stub_post.queue.append(response(200, {"access_token": rotated,
                                              "refresh_token": "r2"}))

        client.upstream.queue.append(FakeUpstream(status=401))
        client.upstream.queue.append(FakeUpstream(chunks=[b"recovered"]))

        resp = await client.post("/responses", json={"model": "m", "input": "hi"})
        assert await resp.read() == b"recovered"
        assert len(client.upstream.calls) == 2
        assert client.upstream.calls[1]["headers"]["Authorization"] == f"Bearer {rotated}"

    async def test_unauthenticated_returns_401(self, client):
        resp = await client.post("/responses", json={"model": "m", "input": "hi"})
        assert resp.status == 401
        assert (await resp.json())["error"]["code"] == "auth_missing"

    async def test_rate_limited_surfaces_as_429_not_401(self, client, signed_in,
                                                        monkeypatch):
        def _rate_limited(**kwargs):
            raise AuthError("quota exhausted", code="rate_limited")

        monkeypatch.setattr(oauth, "resolve_credentials", _rate_limited)
        resp = await client.post("/responses", json={"model": "m", "input": "hi"})
        # A 401 here would send clients into a pointless re-login loop.
        assert resp.status == 429

    async def test_invalid_json_is_rejected(self, client, signed_in):
        resp = await client.post("/responses", data=b"{not json",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400

    async def test_releases_the_upstream_connection(self, client, signed_in):
        upstream = FakeUpstream(chunks=[b"x"])
        client.upstream.queue.append(upstream)
        await client.post("/responses", json={"model": "m", "input": "hi"})
        assert upstream.released is True


# --------------------------------------------------------------------------
# /chat/completions translation
# --------------------------------------------------------------------------

class TestChatCompletionsNonStreaming:
    async def test_collapses_the_stream_into_one_body(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=sse([
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
        ])))
        resp = await client.post("/chat/completions", json={
            "model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "Hello"
        assert body["choices"][0]["finish_reason"] == "stop"

    async def test_translates_the_request_upstream(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=sse([])))
        await client.post("/chat/completions", json={
            "model": "m",
            "messages": [{"role": "system", "content": "be terse"},
                         {"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "echo"}}],
        })
        sent = client.upstream.calls[0]["json"]
        assert client.upstream.calls[0]["url"].endswith("/responses")
        assert sent["input"][0] == {"role": "developer", "content": "be terse"}
        assert sent["tools"] == [{"type": "function", "name": "echo"}]

    async def test_tool_calls_are_returned(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=sse([
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "i1",
                      "call_id": "call-1", "name": "echo_shout"}},
            {"type": "response.function_call_arguments.done",
             "item_id": "i1", "arguments": '{"text": "hi"}'},
        ])))
        body = await (await client.post("/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "shout hi"}],
        })).json()

        choice = body["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"] == [{
            "id": "call-1", "type": "function",
            "function": {"name": "echo_shout", "arguments": '{"text": "hi"}'},
        }]

    async def test_upstream_error_is_surfaced_with_detail(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(
            status=400, body=b'{"error": "Input must be a list"}'))
        resp = await client.post("/chat/completions", json={"model": "m", "messages": []})
        assert resp.status == 400
        assert "Input must be a list" in (await resp.json())["error"]["message"]


class TestChatCompletionsStreaming:
    async def _collect(self, resp) -> list[dict]:
        raw = (await resp.read()).decode()
        return [json.loads(line[6:]) for line in raw.splitlines()
                if line.startswith("data: ") and line[6:].strip() != "[DONE]"]

    async def test_emits_role_then_content_then_finish(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=sse([
            {"type": "response.output_text.delta", "delta": "hi"},
        ])))
        resp = await client.post("/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True,
        })
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")

        frames = await self._collect(resp)
        assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert frames[1]["choices"][0]["delta"] == {"content": "hi"}
        assert frames[-1]["choices"][0]["finish_reason"] == "stop"
        assert (await resp.read()).decode().endswith("data: [DONE]\n\n")

    async def test_streams_tool_calls_with_indices(self, client, signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=sse([
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "i1",
                      "call_id": "call-1", "name": "echo"}},
            {"type": "response.function_call_arguments.delta",
             "item_id": "i1", "delta": '{"a": 1}'},
        ])))
        resp = await client.post("/chat/completions", json={
            "model": "m", "messages": [], "stream": True,
        })
        frames = await self._collect(resp)

        announced = frames[1]["choices"][0]["delta"]["tool_calls"][0]
        assert announced == {"index": 0, "id": "call-1", "type": "function",
                             "function": {"name": "echo", "arguments": ""}}
        streamed = frames[2]["choices"][0]["delta"]["tool_calls"][0]
        assert streamed == {"index": 0, "function": {"arguments": '{"a": 1}'}}
        assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"

    async def test_deltas_reach_the_client_before_the_response_ends(self, client,
                                                                    signed_in):
        """Text must arrive as it is generated, not buffered to the end.

        A single write at the end would still pass every other streaming test
        here, so this one asserts on arrival *time*.
        """
        import asyncio

        class SlowContent:
            def __aiter__(self):
                async def gen():
                    for word in ("alpha", "beta", "gamma"):
                        await asyncio.sleep(0.1)
                        yield (f'data: {{"type": "response.output_text.delta",'
                               f' "delta": "{word}"}}\n').encode()
                    yield b"data: [DONE]\n"
                return gen()

        upstream = FakeUpstream()
        upstream.content = SlowContent()
        client.upstream.queue.append(upstream)

        loop = asyncio.get_running_loop()
        start = loop.time()
        resp = await client.post("/chat/completions", json={
            "model": "m", "messages": [], "stream": True,
        })

        arrivals = []
        async for chunk in resp.content:
            if b'"content"' in chunk:
                arrivals.append(loop.time() - start)

        assert len(arrivals) == 3, f"expected 3 content frames, got {arrivals}"
        # The first word must land well before the last — proof of streaming.
        assert arrivals[0] < arrivals[-1] - 0.1, f"frames arrived together: {arrivals}"

    async def test_malformed_upstream_frames_do_not_kill_the_stream(self, client,
                                                                    signed_in):
        client.upstream.queue.append(FakeUpstream(chunks=[
            b": keepalive\n",
            b"data: {broken\n",
            b'data: {"type": "response.output_text.delta", "delta": "ok"}\n',
            b"data: [DONE]\n",
        ]))
        resp = await client.post("/chat/completions", json={
            "model": "m", "messages": [], "stream": True,
        })
        frames = await self._collect(resp)
        assert any(f["choices"][0]["delta"].get("content") == "ok" for f in frames)


# --------------------------------------------------------------------------
# Login API
# --------------------------------------------------------------------------

class TestLoginApi:
    async def test_status_reports_unauthenticated(self, client):
        body = await (await client.get("/auth/status")).json()
        assert body["authenticated"] is False

    async def test_status_reports_signed_in(self, client, signed_in):
        body = await (await client.get("/auth/status")).json()
        assert body["authenticated"] is True
        assert body["account_id"] == "acct-123"

    async def test_login_start_returns_url_and_code(self, client, monkeypatch,
                                                    stub_post):
        from .conftest import response

        stub_post.queue.append(response(200, {"user_code": "ABCD-1234",
                                              "device_auth_id": "dev-1",
                                              "interval": 5}))
        body = await (await client.post("/auth/login/start")).json()
        assert body["status"] == "pending"
        assert body["user_code"] == "ABCD-1234"
        assert body["verification_uri"] == oauth.VERIFICATION_URI
        assert body["interval"] == 5
        assert body["device_auth_id"] == "dev-1"

    async def test_poll_pending_then_complete_saves_tokens(self, client, stub_post,
                                                           fresh_token):
        from .conftest import response

        stub_post.queue.append(response(200, {"user_code": "A", "device_auth_id": "dev-1"}))
        started = await (await client.post("/auth/login/start")).json()

        stub_post.queue.append(response(403, {}))
        pending = await (await client.post("/auth/login/poll", json={
            "device_auth_id": started["device_auth_id"]})).json()
        assert pending["status"] == "pending"

        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"access_token": fresh_token,
                                              "refresh_token": "r1"}))
        done = await (await client.post("/auth/login/poll", json={
            "device_auth_id": started["device_auth_id"]})).json()

        assert done["status"] == "complete"
        assert done["account_id"] == "acct-123"
        assert oauth.read_tokens()["tokens"]["access_token"] == fresh_token

    async def test_poll_never_returns_the_token(self, client, stub_post, fresh_token):
        from .conftest import response

        stub_post.queue.append(response(200, {"user_code": "A", "device_auth_id": "d"}))
        started = await (await client.post("/auth/login/start")).json()
        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"access_token": fresh_token,
                                              "refresh_token": "r1"}))
        text = await (await client.post("/auth/login/poll", json={
            "device_auth_id": started["device_auth_id"]})).text()
        assert fresh_token not in text
        assert "r1" not in text

    async def test_poll_with_unknown_id_is_404(self, client):
        resp = await client.post("/auth/login/poll", json={"device_auth_id": "nope"})
        assert resp.status == 404
        assert (await resp.json())["error"]["code"] == "unknown_device_auth_id"

    async def test_login_start_surfaces_rate_limiting_as_429(self, client, monkeypatch):
        def _throttled(**kwargs):
            raise AuthError("throttled", code="rate_limited")

        monkeypatch.setattr(oauth, "start_device_login", _throttled)
        assert (await client.post("/auth/login/start")).status == 429

    async def test_pending_logins_are_capped(self, client, monkeypatch):
        counter = {"n": 0}

        def _start(**kwargs):
            import time as _time
            counter["n"] += 1
            return oauth.DeviceAuth(device_auth_id=f"dev-{counter['n']}", user_code="A",
                                    interval=3, expires_at=_time.monotonic() + 60)

        monkeypatch.setattr(oauth, "start_device_login", _start)
        for _ in range(server.MAX_PENDING_LOGINS):
            assert (await client.post("/auth/login/start")).status == 200
        resp = await client.post("/auth/login/start")
        assert resp.status == 429
        assert (await resp.json())["error"]["code"] == "too_many_pending_logins"

    async def test_expired_pending_logins_are_pruned(self, client, monkeypatch):
        import time as _time

        def _start(**kwargs):
            return oauth.DeviceAuth(device_auth_id="dev-old", user_code="A",
                                    interval=3, expires_at=_time.monotonic() - 1)

        monkeypatch.setattr(oauth, "start_device_login", _start)
        await client.post("/auth/login/start")
        # The expired entry must not occupy a slot or be pollable.
        resp = await client.post("/auth/login/poll", json={"device_auth_id": "dev-old"})
        assert resp.status == 404

    async def test_import_from_codex_cli(self, client, fresh_token):
        path = oauth.codex_cli_auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tokens": {"access_token": fresh_token,
                                               "refresh_token": "r1"}}))
        body = await (await client.post("/auth/import")).json()
        assert body["status"] == "imported"
        assert oauth.read_tokens()["tokens"]["access_token"] == fresh_token

    async def test_import_without_cli_tokens_is_404(self, client):
        resp = await client.post("/auth/import")
        assert resp.status == 404
        assert (await resp.json())["error"]["code"] == "codex_cli_tokens_unavailable"

    async def test_refresh_endpoint_rotates_and_persists(self, client, signed_in,
                                                         stub_post):
        from .conftest import make_jwt, response
        import time as _time

        rotated = make_jwt(exp=_time.time() + 7200)
        stub_post.queue.append(response(200, {"access_token": rotated,
                                              "refresh_token": "r2"}))
        body = await (await client.post("/auth/refresh")).json()
        assert body["status"] == "refreshed"
        assert oauth.read_tokens()["tokens"]["refresh_token"] == "r2"

    async def test_logout_clears_credentials(self, client, signed_in):
        assert (await (await client.post("/auth/logout")).json())["status"] == "cleared"
        assert (await client.get("/health")).status == 503

    async def test_logout_is_idempotent(self, client):
        body = await (await client.post("/auth/logout")).json()
        assert body["status"] == "nothing_to_clear"


class TestAdminGuard:
    """CODEX_ADMIN_TOKEN gates /auth/* — these routes sign the gateway in/out."""

    async def test_open_when_unset(self, client):
        assert (await client.get("/auth/status")).status == 200

    async def test_rejects_missing_token(self, client, monkeypatch):
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        resp = await client.get("/auth/status")
        assert resp.status == 403
        assert (await resp.json())["error"]["code"] == "admin_token_required"

    async def test_rejects_wrong_token(self, client, monkeypatch):
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        resp = await client.get("/auth/status",
                                headers={"Authorization": "Bearer wrong"})
        assert resp.status == 403

    async def test_accepts_correct_token(self, client, monkeypatch):
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        resp = await client.get("/auth/status",
                                headers={"Authorization": "Bearer s3cret"})
        assert resp.status == 200

    async def test_guard_is_case_insensitive_on_the_scheme(self, client, monkeypatch):
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        resp = await client.get("/auth/status",
                                headers={"Authorization": "bearer s3cret"})
        assert resp.status == 200

    async def test_does_not_gate_the_provider_surface(self, client, monkeypatch):
        # Bifrost has no admin token; gating /models would break routing.
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        assert (await client.get("/models")).status == 200
        assert (await client.get("/health")).status in (200, 503)

    async def test_completions_stay_open_so_this_is_not_usage_control(
            self, client, monkeypatch, signed_in):
        """The guard protects credential management, not spend.

        /chat/completions cannot require a token — Bifrost has none to send.
        """
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        client.upstream.queue.append(FakeUpstream(chunks=sse([])))
        resp = await client.post("/chat/completions",
                                 json={"model": "m", "messages": []})
        assert resp.status == 200

    async def test_logout_is_gated(self, client, monkeypatch, signed_in):
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        assert (await client.post("/auth/logout")).status == 403
        assert oauth.credential_status()["authenticated"] is True

    async def test_login_start_is_gated(self, client, monkeypatch):
        monkeypatch.setenv("CODEX_ADMIN_TOKEN", "s3cret")
        assert (await client.post("/auth/login/start")).status == 403


class TestRouting:
    @pytest.mark.parametrize("path", ["/responses", "/v1/responses",
                                      "/chat/completions", "/v1/chat/completions"])
    async def test_post_routes_exist(self, client, path):
        # Unauthenticated 401 proves the route resolved to a handler.
        assert (await client.post(path, json={"model": "m"})).status == 401

    @pytest.mark.parametrize("path", ["/models", "/v1/models", "/health",
                                      "/auth/status"])
    async def test_get_routes_exist(self, client, path):
        assert (await client.get(path)).status != 404

    async def test_unknown_path_is_404(self, client):
        assert (await client.get("/nope")).status == 404

    def test_readme_documents_every_route(self):
        """The README's API table is the only place the surface is written down.

        Without this, adding a route silently leaves it undocumented.
        """
        import re
        from pathlib import Path

        readme = Path(__file__).resolve().parents[1] / "README.md"
        documented = set(re.findall(r"^\| `(GET|POST)` \| `([^`]+)` \|",
                                    readme.read_text(), re.MULTILINE))

        registered = set()
        for resource in server.build_app().router.resources():
            path = resource.canonical
            for route in resource:
                if route.method != "HEAD":
                    registered.add((route.method, path))

        # /v1 aliases are covered by a sentence rather than their own rows.
        registered = {(m, p) for m, p in registered if not p.startswith("/v1/")}

        assert registered == documented, (
            f"undocumented: {sorted(registered - documented)}\n"
            f"stale in README: {sorted(documented - registered)}"
        )


class TestRunPort:
    """The listen port: `--port` beats the env var, which beats the default."""

    @pytest.fixture
    def served(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(server.web, "run_app",
                            lambda app, **kw: seen.update(kw))
        monkeypatch.setattr(server, "build_app", lambda: None)
        return seen

    def test_defaults_to_8085(self, served, monkeypatch):
        monkeypatch.delenv("CODEX_GATEWAY_PORT", raising=False)
        assert server.run() == 0
        assert served["port"] == 8085

    def test_env_var_overrides_default(self, served, monkeypatch):
        monkeypatch.setenv("CODEX_GATEWAY_PORT", "9100")
        assert server.run() == 0
        assert served["port"] == 9100

    def test_argument_overrides_env_var(self, served, monkeypatch):
        monkeypatch.setenv("CODEX_GATEWAY_PORT", "9100")
        assert server.run(9000) == 0
        assert served["port"] == 9000
