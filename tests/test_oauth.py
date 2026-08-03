"""Token store, JWT decoding, refresh, and the device-code login flow."""
from __future__ import annotations

import email
import io
import json
import os
import stat
import time
import urllib.error
from pathlib import Path

import pytest

from codex_gateway import oauth
from codex_gateway.oauth import AuthError

from .conftest import make_jwt, response

# Captured before conftest's no_network fixture swaps the module attribute, so
# TestHttpPost can exercise the real implementation.
REAL_HTTP_POST = oauth.http_post


class _FakeResponse:
    """Minimal stand-in for the context manager urlopen returns."""

    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------

class TestJwtClaims:
    def test_reads_account_id(self, fresh_token):
        assert oauth.account_id_from_token(fresh_token) == "acct-123"

    def test_falls_back_to_plain_account_id_claim(self):
        import base64

        claims = {"https://api.openai.com/auth": {"account_id": "acct-legacy"}}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        assert oauth.account_id_from_token(f"h.{payload}.s") == "acct-legacy"

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", None, 42,
                                       "a.!!!not-base64!!!.c"])
    def test_malformed_tokens_yield_no_claims(self, token):
        assert oauth.decode_jwt_claims(token) == {}
        assert oauth.account_id_from_token(token) is None

    def test_expiring_token_detected_within_skew(self):
        token = make_jwt(exp=time.time() + 30)
        assert oauth.access_token_is_expiring(token, skew_seconds=120) is True
        assert oauth.access_token_is_expiring(token, skew_seconds=0) is False

    def test_expired_token_is_expiring_even_with_no_skew(self, expired_token):
        assert oauth.access_token_is_expiring(expired_token, 0) is True

    def test_token_without_exp_is_not_treated_as_expiring(self):
        # Refreshing on every request would burn the single-use refresh token.
        assert oauth.access_token_is_expiring(make_jwt(exp=None)) is False


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class TestStore:
    def test_read_without_store_raises_relogin(self):
        with pytest.raises(AuthError) as exc:
            oauth.read_tokens()
        assert exc.value.code == "auth_missing"
        assert exc.value.relogin_required is True

    def test_save_then_read_roundtrip(self, fresh_token):
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        data = oauth.read_tokens()
        assert data["tokens"]["access_token"] == fresh_token
        assert data["tokens"]["refresh_token"] == "r1"
        assert data["last_refresh"]

    def test_saved_file_is_owner_only(self, fresh_token):
        path = oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"credential file is {oct(mode)}, expected 0o600"

    def test_no_temp_files_left_behind(self, fresh_token):
        path = oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        leftovers = [p.name for p in path.parent.iterdir() if ".tmp." in p.name]
        assert leftovers == []

    def test_save_preserves_unrelated_providers(self, fresh_token):
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        path = oauth.store_path()
        store = json.loads(path.read_text())
        store["providers"]["some-other-provider"] = {"tokens": {"access_token": "keep"}}
        path.write_text(json.dumps(store))

        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r2"})
        after = json.loads(path.read_text())
        assert after["providers"]["some-other-provider"]["tokens"]["access_token"] == "keep"
        assert after["providers"]["openai-codex"]["tokens"]["refresh_token"] == "r2"

    def test_save_rejects_empty_access_token(self):
        with pytest.raises(AuthError) as exc:
            oauth.save_tokens({"access_token": "", "refresh_token": "r"})
        assert exc.value.code == "save_missing_access_token"

    def test_partial_token_pair_is_not_usable(self, fresh_token):
        oauth.store_path().parent.mkdir(parents=True, exist_ok=True)
        oauth.store_path().write_text(json.dumps({
            "providers": {"openai-codex": {"tokens": {"access_token": fresh_token}}}
        }))
        with pytest.raises(AuthError):
            oauth.read_tokens()

    def test_unreadable_store_raises_instead_of_clobbering(self):
        path = oauth.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")
        with pytest.raises(AuthError) as exc:
            oauth.read_tokens()
        assert exc.value.code == "store_unreadable"
        # The only copy of a live credential must survive a parse failure.
        assert path.read_text() == "{ this is not json"

    def test_clear_tokens(self, fresh_token):
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        assert oauth.clear_tokens() is True
        assert oauth.clear_tokens() is False
        with pytest.raises(AuthError):
            oauth.read_tokens()

    def test_store_home_is_configurable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_GATEWAY_HOME", str(tmp_path / "elsewhere"))
        assert oauth.store_path() == tmp_path / "elsewhere" / "auth.json"

    def test_store_home_defaults_to_data(self, monkeypatch):
        monkeypatch.delenv("CODEX_GATEWAY_HOME")
        assert oauth.store_path() == Path("/data/auth.json")


class TestCredentialPoolCompat:
    """A volume written by older tooling may hold tokens only in the pool."""

    def _write_pool(self, entries):
        path = oauth.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"providers": {},
                                    "credential_pool": {"openai-codex": entries}}))

    def test_pool_token_is_read_as_fallback(self, fresh_token):
        self._write_pool([{"access_token": fresh_token}])
        data = oauth.read_tokens()
        assert data["tokens"]["access_token"] == fresh_token
        assert data["source"] == "credential_pool"

    def test_pool_entry_in_cooldown_is_skipped(self, fresh_token):
        self._write_pool([
            {"access_token": "cooling", "last_error_reset_at": time.time() + 600},
            {"access_token": fresh_token},
        ])
        assert oauth.read_tokens()["tokens"]["access_token"] == fresh_token

    def test_singleton_wins_over_pool(self, fresh_token):
        path = oauth.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "providers": {"openai-codex": {"tokens": {"access_token": fresh_token,
                                                      "refresh_token": "r1"}}},
            "credential_pool": {"openai-codex": [{"access_token": "pooled"}]},
        }))
        assert oauth.read_tokens()["tokens"]["access_token"] == fresh_token

    def test_pool_only_credential_has_no_refresh_token(self, fresh_token):
        self._write_pool([{"access_token": fresh_token}])
        assert oauth.read_tokens()["tokens"]["refresh_token"] == ""


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------

class TestRefresh:
    def test_successful_refresh_returns_rotated_pair(self, stub_post, fresh_token):
        stub_post.queue.append(response(200, {"access_token": fresh_token,
                                              "refresh_token": "r2"}))
        result = oauth.refresh_tokens("r1")
        assert result["access_token"] == fresh_token
        assert result["refresh_token"] == "r2"
        assert result["last_refresh"]

        sent = stub_post.calls[0]
        assert sent["url"] == oauth.TOKEN_URL
        assert sent["form"]["grant_type"] == "refresh_token"
        assert sent["form"]["refresh_token"] == "r1"
        assert sent["form"]["client_id"] == oauth.CLIENT_ID

    def test_keeps_old_refresh_token_when_none_rotated(self, stub_post, fresh_token):
        stub_post.queue.append(response(200, {"access_token": fresh_token}))
        assert oauth.refresh_tokens("r1")["refresh_token"] == "r1"

    def test_missing_refresh_token_requires_relogin(self):
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("")
        assert exc.value.code == "missing_refresh_token"
        assert exc.value.relogin_required is True

    def test_429_is_quota_not_a_credential_problem(self, stub_post):
        stub_post.queue.append(response(429, {}, {"Retry-After": "42"}))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.code == "rate_limited"
        # Telling the user to re-login here would be actively wrong.
        assert exc.value.relogin_required is False
        assert "42s" in str(exc.value)

    def test_invalid_grant_requires_relogin(self, stub_post):
        stub_post.queue.append(response(400, {"error": "invalid_grant",
                                              "error_description": "expired"}))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.code == "invalid_grant"
        assert exc.value.relogin_required is True
        assert "expired" in str(exc.value)

    def test_openai_nested_error_shape(self, stub_post):
        stub_post.queue.append(response(400, {"error": {"code": "invalid_token",
                                                        "message": "bad token"}}))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.code == "invalid_token"
        assert "bad token" in str(exc.value)

    def test_reused_refresh_token_explains_the_cause(self, stub_post):
        stub_post.queue.append(response(400, {"error": "refresh_token_reused"}))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.relogin_required is True
        assert "already consumed by another client" in str(exc.value)

    def test_401_forces_relogin_even_with_unknown_error_code(self, stub_post):
        stub_post.queue.append(response(401, {"error": "something_new"}))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.relogin_required is True

    def test_response_without_access_token_is_rejected(self, stub_post):
        stub_post.queue.append(response(200, {"refresh_token": "r2"}))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.code == "refresh_missing_access_token"

    def test_non_json_success_is_rejected(self, stub_post):
        stub_post.queue.append(oauth.HttpResponse(200, b"<html>oops</html>"))
        with pytest.raises(AuthError) as exc:
            oauth.refresh_tokens("r1")
        assert exc.value.code == "refresh_invalid_json"


# --------------------------------------------------------------------------
# resolve_credentials
# --------------------------------------------------------------------------

class TestResolveCredentials:
    def test_returns_stored_token_without_refreshing(self, fresh_token):
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        creds = oauth.resolve_credentials()
        assert creds["api_key"] == fresh_token
        assert creds["account_id"] == "acct-123"
        assert creds["base_url"] == oauth.DEFAULT_BASE_URL
        # no_network would have raised had a refresh been attempted

    def test_refreshes_and_persists_when_expiring(self, stub_post, expired_token):
        oauth.save_tokens({"access_token": expired_token, "refresh_token": "r1"})
        new_token = make_jwt(exp=time.time() + 3600, account_id="acct-new")
        stub_post.queue.append(response(200, {"access_token": new_token,
                                              "refresh_token": "r2"}))

        creds = oauth.resolve_credentials()
        assert creds["api_key"] == new_token
        assert creds["account_id"] == "acct-new"

        # Rotation must be persisted or the next refresh replays a dead token.
        stored = oauth.read_tokens()["tokens"]
        assert stored["access_token"] == new_token
        assert stored["refresh_token"] == "r2"

    def test_force_refresh_ignores_a_healthy_token(self, stub_post, fresh_token):
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        rotated = make_jwt(exp=time.time() + 7200)
        stub_post.queue.append(response(200, {"access_token": rotated,
                                              "refresh_token": "r2"}))
        assert oauth.resolve_credentials(force_refresh=True)["api_key"] == rotated

    def test_refresh_if_expiring_disabled_returns_stale_token(self, expired_token):
        oauth.save_tokens({"access_token": expired_token, "refresh_token": "r1"})
        creds = oauth.resolve_credentials(refresh_if_expiring=False)
        assert creds["api_key"] == expired_token

    def test_base_url_override(self, fresh_token, monkeypatch):
        monkeypatch.setenv("CODEX_BASE_URL", "https://example.test/api/")
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        assert oauth.resolve_credentials()["base_url"] == "https://example.test/api"


class TestCredentialStatus:
    def test_reports_unauthenticated_without_raising(self):
        status = oauth.credential_status()
        assert status["authenticated"] is False
        assert status["code"] == "auth_missing"

    def test_never_returns_the_token_itself(self, fresh_token):
        oauth.save_tokens({"access_token": fresh_token, "refresh_token": "r1"})
        status = oauth.credential_status()
        assert status["authenticated"] is True
        assert status["account_id"] == "acct-123"
        assert status["can_refresh"] is True
        assert fresh_token not in json.dumps(status)
        assert "r1" not in json.dumps(status)

    def test_flags_expiring_credentials(self, expired_token):
        oauth.save_tokens({"access_token": expired_token, "refresh_token": "r1"})
        assert oauth.credential_status()["expiring_soon"] is True

    def test_reports_when_refresh_is_impossible(self, fresh_token):
        path = oauth.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"providers": {},
                                    "credential_pool": {"openai-codex":
                                                        [{"access_token": fresh_token}]}}))
        assert oauth.credential_status()["can_refresh"] is False


# --------------------------------------------------------------------------
# Codex CLI import
# --------------------------------------------------------------------------

class TestCodexCliImport:
    def _write_cli(self, payload):
        path = oauth.codex_cli_auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_missing_file(self):
        assert oauth.import_codex_cli_tokens() is None

    def test_imports_valid_pair(self, fresh_token):
        self._write_cli({"tokens": {"access_token": fresh_token, "refresh_token": "r1"}})
        assert oauth.import_codex_cli_tokens() == {"access_token": fresh_token,
                                                   "refresh_token": "r1"}

    def test_rejects_expired_tokens(self, expired_token):
        # Importing stale tokens leaves the user "signed in" with nothing working.
        self._write_cli({"tokens": {"access_token": expired_token, "refresh_token": "r1"}})
        assert oauth.import_codex_cli_tokens() is None

    def test_rejects_incomplete_pair(self, fresh_token):
        self._write_cli({"tokens": {"access_token": fresh_token}})
        assert oauth.import_codex_cli_tokens() is None

    def test_rejects_malformed_file(self):
        path = oauth.codex_cli_auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert oauth.import_codex_cli_tokens() is None

    def test_does_not_write_to_the_cli_store(self, fresh_token):
        path = self._write_cli({"tokens": {"access_token": fresh_token,
                                           "refresh_token": "r1"}})
        before = path.read_text()
        oauth.import_codex_cli_tokens()
        assert path.read_text() == before


# --------------------------------------------------------------------------
# Device-code login
# --------------------------------------------------------------------------

class TestDeviceLogin:
    def test_start_returns_code_and_url(self, stub_post):
        stub_post.queue.append(response(200, {"user_code": "ABCD-1234",
                                              "device_auth_id": "dev-1",
                                              "interval": 5}))
        auth = oauth.start_device_login()
        assert auth.user_code == "ABCD-1234"
        assert auth.device_auth_id == "dev-1"
        assert auth.interval == 5
        assert auth.verification_uri == oauth.VERIFICATION_URI
        assert stub_post.calls[0]["json"] == {"client_id": oauth.CLIENT_ID}

    def test_start_enforces_minimum_poll_interval(self, stub_post):
        stub_post.queue.append(response(200, {"user_code": "A", "device_auth_id": "d",
                                              "interval": 1}))
        assert oauth.start_device_login().interval == 3

    def test_start_retries_on_429_then_succeeds(self, stub_post):
        slept = []
        stub_post.queue.append(response(429, {}, {"Retry-After": "2"}))
        stub_post.queue.append(response(200, {"user_code": "A", "device_auth_id": "d"}))
        auth = oauth.start_device_login(sleep=slept.append)
        assert auth.user_code == "A"
        assert slept == [2]

    def test_start_gives_up_after_repeated_429(self, stub_post):
        for _ in range(4):
            stub_post.queue.append(response(429, {}, {"Retry-After": "1"}))
        with pytest.raises(AuthError) as exc:
            oauth.start_device_login(sleep=lambda _: None)
        assert exc.value.code == "rate_limited"
        assert "not a credential problem" in str(exc.value)

    def test_start_rejects_incomplete_response(self, stub_post):
        stub_post.queue.append(response(200, {"user_code": "A"}))
        with pytest.raises(AuthError) as exc:
            oauth.start_device_login()
        assert exc.value.code == "device_code_incomplete"

    def _auth(self):
        return oauth.DeviceAuth(device_auth_id="dev-1", user_code="ABCD",
                                interval=3, expires_at=time.monotonic() + 60)

    @pytest.mark.parametrize("status", [403, 404])
    def test_poll_pending_returns_none(self, stub_post, status):
        stub_post.queue.append(response(status, {}))
        assert oauth.poll_device_login(self._auth()) is None

    def test_poll_success_exchanges_for_tokens(self, stub_post, fresh_token):
        stub_post.queue.append(response(200, {"authorization_code": "code-1",
                                              "code_verifier": "verifier-1"}))
        stub_post.queue.append(response(200, {"access_token": fresh_token,
                                              "refresh_token": "r1"}))
        tokens = oauth.poll_device_login(self._auth())
        assert tokens["access_token"] == fresh_token
        assert tokens["refresh_token"] == "r1"

        exchange = stub_post.calls[1]
        assert exchange["url"] == oauth.TOKEN_URL
        assert exchange["form"]["grant_type"] == "authorization_code"
        assert exchange["form"]["code"] == "code-1"
        assert exchange["form"]["code_verifier"] == "verifier-1"
        assert exchange["form"]["redirect_uri"] == oauth.REDIRECT_URI

    def test_poll_unexpected_status_raises(self, stub_post):
        stub_post.queue.append(response(500, {}))
        with pytest.raises(AuthError) as exc:
            oauth.poll_device_login(self._auth())
        assert exc.value.code == "device_code_poll_failed"

    def test_exchange_without_access_token_raises(self, stub_post):
        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"refresh_token": "r"}))
        with pytest.raises(AuthError) as exc:
            oauth.poll_device_login(self._auth())
        assert exc.value.code == "token_exchange_no_access_token"

    def test_wait_polls_until_approved(self, stub_post, fresh_token):
        stub_post.queue.append(response(403, {}))
        stub_post.queue.append(response(403, {}))
        stub_post.queue.append(response(200, {"authorization_code": "c",
                                              "code_verifier": "v"}))
        stub_post.queue.append(response(200, {"access_token": fresh_token,
                                              "refresh_token": "r1"}))
        slept = []
        tokens = oauth.wait_for_tokens(self._auth(), sleep=slept.append)
        assert tokens["access_token"] == fresh_token
        assert slept == [3, 3, 3]

    def test_wait_times_out_when_code_expires(self):
        expired = oauth.DeviceAuth(device_auth_id="d", user_code="A", interval=3,
                                   expires_at=time.monotonic() - 1)
        with pytest.raises(AuthError) as exc:
            oauth.wait_for_tokens(expired, sleep=lambda _: None)
        assert exc.value.code == "device_code_timeout"

    def test_public_payload_omits_nothing_the_user_needs(self):
        payload = self._auth().public()
        assert payload["user_code"] == "ABCD"
        assert payload["verification_uri"] == oauth.VERIFICATION_URI
        assert payload["interval"] == 3
        assert payload["expires_in"] > 0


class TestHttpPost:
    """The real http_post, with only urlopen stubbed.

    ``no_network`` replaces the module attribute, so these call the function
    object captured at import time instead.
    """

    def test_network_failure_becomes_auth_error(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(oauth.urllib.request, "urlopen", _boom)
        with pytest.raises(AuthError) as exc:
            REAL_HTTP_POST(oauth.TOKEN_URL, form={"a": "b"})
        assert exc.value.code == "network_error"

    def test_http_error_is_returned_as_a_response_not_raised(self, monkeypatch):
        # Callers branch on status; a raise would force try/except everywhere.
        def _raise_http_error(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://x", code=429, msg="Too Many Requests",
                hdrs=email.message_from_string("Retry-After: 7"),
                fp=io.BytesIO(b'{"error": "slow_down"}'),
            )

        monkeypatch.setattr(oauth.urllib.request, "urlopen", _raise_http_error)
        resp = REAL_HTTP_POST(oauth.TOKEN_URL, form={"a": "b"})
        assert resp.status == 429
        assert resp.json() == {"error": "slow_down"}
        assert resp.headers["Retry-After"] == "7"

    def test_form_and_json_bodies_are_encoded_differently(self, monkeypatch):
        seen = {}

        def _capture(req, timeout=None):
            seen["content_type"] = req.headers["Content-type"]
            seen["data"] = req.data
            return _FakeResponse(200, b"{}")

        monkeypatch.setattr(oauth.urllib.request, "urlopen", _capture)

        REAL_HTTP_POST("https://x", form={"grant_type": "refresh_token"})
        assert seen["content_type"] == "application/x-www-form-urlencoded"
        assert seen["data"] == b"grant_type=refresh_token"

        REAL_HTTP_POST("https://x", json_body={"client_id": "abc"})
        assert seen["content_type"] == "application/json"
        assert json.loads(seen["data"]) == {"client_id": "abc"}

    def test_non_json_body_decodes_to_none(self):
        assert oauth.HttpResponse(200, b"<html>").json() is None
