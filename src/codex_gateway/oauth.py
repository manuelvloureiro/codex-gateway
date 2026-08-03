"""ChatGPT (Codex) OAuth: token store, refresh, and device-code login.

The device-code flow, the refresh semantics, and the on-disk format follow the
Hermes CLI's implementation (MIT). Reimplemented here so the service carries no
dependency on it; the store stays wire- and format-compatible.

The store layout:

    {"version": 1,
     "providers": {"openai-codex": {"tokens": {"access_token": ..., "refresh_token": ...},
                                    "last_refresh": ..., "auth_mode": "chatgpt"}}}

A ``credential_pool.openai-codex`` list is also read as a fallback, for stores
written by older tooling. We never write one.

Synchronous on purpose: the CLI calls it directly and the server calls it
through ``asyncio.to_thread``, so there is one implementation rather than a
sync and an async copy.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

log = logging.getLogger("codex_gateway.oauth")

ISSUER = "https://auth.openai.com"
TOKEN_URL = f"{ISSUER}/oauth/token"
DEVICE_CODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
VERIFICATION_URI = f"{ISSUER}/codex/device"
REDIRECT_URI = f"{ISSUER}/deviceauth/callback"

# The public Codex client id. Not a secret — it is baked into the Codex CLI.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
PROVIDER_ID = "openai-codex"
STORE_VERSION = 1

# Refresh this many seconds before the JWT's own expiry, so a token cannot die
# mid-flight on a long streaming response.
REFRESH_SKEW_SECONDS = 120
DEVICE_LOGIN_TIMEOUT_SECONDS = 15 * 60

USER_AGENT = "codex-gateway/1.0"

# Serialises refresh across the server's concurrent requests. Single container,
# single process — a thread lock is the whole requirement. It is reentrant
# because resolve_credentials() holds it across a save.
_store_lock = threading.RLock()


class AuthError(RuntimeError):
    """An auth failure. ``relogin_required`` distinguishes "your credentials are
    gone, sign in again" from "upstream said no, try later"."""

    def __init__(self, message: str, *, code: str = "auth_error",
                 relogin_required: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


# --------------------------------------------------------------------------
# JWT (payload only — we never verify; the backend does that)
# --------------------------------------------------------------------------

def decode_jwt_claims(token: Any) -> dict[str, Any]:
    """Decode a JWT's payload without verifying it.

    Used only to read ``exp`` and the account id. A malformed token yields {},
    which callers treat as "no information", never as "valid".
    """
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001 - a malformed token is simply unreadable
        return {}


def account_id_from_token(token: Any) -> str | None:
    """Decode the ChatGPT-Account-ID the Codex CLI sends as a header."""
    auth_claims = decode_jwt_claims(token).get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return None
    return auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id")


def access_token_is_expiring(token: Any, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    """True when the token expires within ``skew_seconds``.

    A token with no readable ``exp`` is reported as NOT expiring: we cannot
    prove it is stale, and refreshing on every request would burn the
    single-use refresh token.
    """
    exp = decode_jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return float(exp) <= time.time() + max(0, int(skew_seconds))


# --------------------------------------------------------------------------
# HTTP (stdlib; one seam so tests never touch the network)
# --------------------------------------------------------------------------

@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except Exception:  # noqa: BLE001
            return None


def http_post(url: str, *, json_body: dict | None = None,
              form: dict | None = None, timeout: float = 15.0) -> HttpResponse:
    """POST JSON or form-encoded. Returns non-2xx as a value, not an exception,
    because every caller here needs to branch on the status code."""
    if json_body is not None:
        data = json.dumps(json_body).encode()
        content_type = "application/json"
    else:
        data = urlencode(form or {}).encode()
        content_type = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": content_type,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"", dict(exc.headers or {}))
    except Exception as exc:  # noqa: BLE001 - DNS, TLS, timeouts
        raise AuthError(f"request to {url} failed: {exc}", code="network_error") from exc


def _retry_after_seconds(headers: dict[str, str]) -> int | None:
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return max(0, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def store_path() -> Path:
    """The auth.json holding the token pair."""
    home = Path((os.getenv("CODEX_GATEWAY_HOME") or "/data").strip()).expanduser()
    return home / "auth.json"


def _load_store() -> dict[str, Any]:
    path = store_path()
    if not path.is_file():
        return {"version": STORE_VERSION, "providers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # Do not clobber a file we failed to parse — it may be recoverable by
        # hand, and it is the user's only copy of a live credential.
        raise AuthError(
            f"credential store at {path} is not readable JSON: {exc}",
            code="store_unreadable",
        ) from exc
    if not isinstance(raw, dict):
        raise AuthError(f"credential store at {path} is not an object",
                        code="store_invalid")
    raw.setdefault("providers", {})
    return raw


def _write_store(store: dict[str, Any]) -> Path:
    """Atomically write the store 0600.

    O_EXCL + fdopen creates the temp file already-private; a write-then-chmod
    would briefly expose live tokens at the process umask.
    """
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store["version"] = STORE_VERSION
    store["updated_at"] = _utc_now()

    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(store, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _pool_access_token(store: dict[str, Any]) -> str:
    """Read a token from the legacy ``credential_pool`` list.

    Older tooling kept this alongside the singleton, so a volume it seeded may
    hold tokens only here.
    """
    pool = store.get("credential_pool")
    entries = pool.get(PROVIDER_ID) if isinstance(pool, dict) else None
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("access_token") or "").strip()
        if not token:
            continue
        # Skip an entry parked in a quota cooldown by the old code.
        reset_at = entry.get("last_error_reset_at")
        if isinstance(reset_at, (int, float)) and reset_at > time.time():
            continue
        return token
    return ""


def read_tokens() -> dict[str, Any]:
    """Return ``{"tokens": {...}, "last_refresh": ...}`` or raise AuthError."""
    with _store_lock:
        store = _load_store()
    providers = store.get("providers")
    state = providers.get(PROVIDER_ID) if isinstance(providers, dict) else None

    if isinstance(state, dict) and isinstance(state.get("tokens"), dict):
        tokens = state["tokens"]
        access = str(tokens.get("access_token") or "").strip()
        refresh = str(tokens.get("refresh_token") or "").strip()
        if access and refresh:
            return {"tokens": dict(tokens), "last_refresh": state.get("last_refresh")}

    pooled = _pool_access_token(store)
    if pooled:
        # Pool entries carry no refresh token, so this credential cannot be
        # renewed — it works until it expires, then requires a fresh login.
        return {"tokens": {"access_token": pooled, "refresh_token": ""},
                "last_refresh": None, "source": "credential_pool"}

    raise AuthError(
        "no ChatGPT credentials stored — run `python -m codex_gateway.login` "
        "or POST /auth/login/start",
        code="auth_missing", relogin_required=True,
    )


def save_tokens(tokens: dict[str, str], *, last_refresh: str | None = None,
                label: str | None = None) -> Path:
    """Persist the token pair, preserving unrelated providers in the store."""
    access = str((tokens or {}).get("access_token") or "").strip()
    if not access:
        raise AuthError("refusing to save credentials with no access_token",
                        code="save_missing_access_token")
    with _store_lock:
        store = _load_store()
        providers = store.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = store["providers"] = {}
        state = providers.get(PROVIDER_ID)
        state = dict(state) if isinstance(state, dict) else {}
        state["tokens"] = dict(tokens)
        state["last_refresh"] = last_refresh or _utc_now()
        state["auth_mode"] = "chatgpt"
        if label and label.strip():
            state["label"] = label.strip()
        providers[PROVIDER_ID] = state
        store["active_provider"] = PROVIDER_ID
        return _write_store(store)


def clear_tokens() -> bool:
    """Forget stored credentials. True if there was something to forget."""
    with _store_lock:
        store = _load_store()
        providers = store.get("providers")
        removed = isinstance(providers, dict) and providers.pop(PROVIDER_ID, None) is not None
        pool = store.get("credential_pool")
        if isinstance(pool, dict) and pool.pop(PROVIDER_ID, None) is not None:
            removed = True
        if removed:
            _write_store(store)
        return removed


def base_url() -> str:
    return (os.getenv("CODEX_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------

def refresh_tokens(refresh_token: str, *, timeout: float = 20.0) -> dict[str, str]:
    """Exchange a refresh token for a new pair. Does not touch the store.

    OAuth refresh tokens here are single-use: the response usually carries a
    replacement, and the old one is dead the moment this succeeds. Callers must
    persist the result or the next refresh replays a consumed token.
    """
    refresh_token = str(refresh_token or "").strip()
    if not refresh_token:
        raise AuthError(
            "stored credentials have no refresh_token — sign in again",
            code="missing_refresh_token", relogin_required=True,
        )

    resp = http_post(TOKEN_URL, form={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }, timeout=max(5.0, timeout))

    if resp.status == 429:
        # Quota, not credentials. Telling the user to re-login here would be
        # actively wrong — a new token cannot lift a rate limit.
        retry = _retry_after_seconds(resp.headers)
        hint = f" retry after {retry}s." if retry is not None else " retry once the limit resets."
        raise AuthError(f"ChatGPT quota exhausted (429);{hint} Credentials are still valid.",
                        code="rate_limited", relogin_required=False)

    if resp.status != 200:
        code, message = _refresh_error(resp)
        relogin = code in {"invalid_grant", "invalid_token", "invalid_request",
                           "refresh_token_reused"} or resp.status in {401, 403}
        if code == "refresh_token_reused":
            message = ("the refresh token was already consumed by another client "
                       "(Codex CLI, VS Code extension, or a second gateway). "
                       "Sign in again, or import the CLI's tokens.")
        raise AuthError(message, code=code, relogin_required=relogin)

    payload = resp.json()
    if not isinstance(payload, dict):
        raise AuthError("token refresh returned invalid JSON",
                        code="refresh_invalid_json", relogin_required=True)

    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise AuthError("token refresh response had no access_token",
                        code="refresh_missing_access_token", relogin_required=True)

    rotated = str(payload.get("refresh_token") or "").strip()
    return {
        "access_token": access,
        "refresh_token": rotated or refresh_token,
        "last_refresh": _utc_now(),
    }


def _refresh_error(resp: HttpResponse) -> tuple[str, str]:
    """Pull a code and message out of either OpenAI's or OAuth's error shape."""
    code = "refresh_failed"
    message = f"token refresh failed with status {resp.status}"
    payload = resp.json()
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):  # {"error": {"code": ..., "message": ...}}
            nested = err.get("code") or err.get("type")
            if isinstance(nested, str) and nested.strip():
                code = nested.strip()
            text = err.get("message")
            if isinstance(text, str) and text.strip():
                message = f"token refresh failed: {text.strip()}"
        elif isinstance(err, str) and err.strip():  # {"error": "...", "error_description": ...}
            code = err.strip()
            text = payload.get("error_description") or payload.get("message")
            if isinstance(text, str) and text.strip():
                message = f"token refresh failed: {text.strip()}"
    return code, message


# --------------------------------------------------------------------------
# Codex CLI import
# --------------------------------------------------------------------------

def codex_cli_auth_path() -> Path:
    home = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    return Path(home).expanduser() / "auth.json"


def import_codex_cli_tokens() -> dict[str, str] | None:
    """Read a usable token pair from the Codex CLI's own store, or None.

    Never writes to the CLI's file — it is not ours, and the CLI treats it as
    authoritative. Expired tokens are rejected rather than imported, so a
    successful import always means working credentials.
    """
    path = codex_cli_auth_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return None
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    if not access or not refresh:
        return None
    if access_token_is_expiring(access, 0):
        log.debug("Codex CLI tokens at %s are expired — not importing", path)
        return None
    return dict(tokens)


# --------------------------------------------------------------------------
# Device-code login
# --------------------------------------------------------------------------

@dataclass
class DeviceAuth:
    """A device-code login in progress.

    Split from the polling loop so the same flow serves both front ends: the
    CLI blocks on wait_for_tokens(), while the HTTP API hands the fields to a
    client that polls at its own pace.
    """
    device_auth_id: str
    user_code: str
    interval: int
    expires_at: float
    verification_uri: str = VERIFICATION_URI

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def public(self) -> dict[str, Any]:
        """Fields safe to hand a client. The user_code is meant to be shown."""
        return {
            "device_auth_id": self.device_auth_id,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "interval": self.interval,
            "expires_in": max(0, int(self.expires_at - time.monotonic())),
        }


def start_device_login(*, timeout: float = 15.0, max_attempts: int = 4,
                       sleep=None) -> DeviceAuth:
    """Request a device code.

    OpenAI throttles this endpoint per IP/account, so a 429 is retried with
    backoff (honouring Retry-After) before being surfaced — a bare 429 here
    reads as a credential problem when it is just a queue.

    ``sleep`` is resolved at call time, not bound as a default, so patching
    ``time.sleep`` actually takes effect.
    """
    sleep = sleep or time.sleep
    resp = None
    for attempt in range(1, max_attempts + 1):
        resp = http_post(DEVICE_CODE_URL, json_body={"client_id": CLIENT_ID}, timeout=timeout)
        if resp.status != 429:
            break
        if attempt < max_attempts:
            delay = _retry_after_seconds(resp.headers)
            delay = max(1, min(int(delay if delay is not None else 2 ** attempt), 60))
            log.warning("OpenAI is throttling login requests (429); retrying in %ds", delay)
            sleep(delay)

    if resp is not None and resp.status == 429:
        retry = _retry_after_seconds(resp.headers)
        hint = f" Try again in about {retry}s." if retry is not None else " Wait a minute and retry."
        raise AuthError(
            f"OpenAI is rate-limiting Codex login requests (429). This is a temporary "
            f"throttle on their side, not a credential problem.{hint}",
            code="rate_limited",
        )
    if resp is None or resp.status != 200:
        status = resp.status if resp is not None else "unknown"
        raise AuthError(f"device code request returned status {status}",
                        code="device_code_request_failed")

    data = resp.json()
    if not isinstance(data, dict):
        raise AuthError("device code response was not JSON", code="device_code_invalid")

    user_code = str(data.get("user_code") or "").strip()
    device_auth_id = str(data.get("device_auth_id") or "").strip()
    if not user_code or not device_auth_id:
        raise AuthError("device code response was missing user_code/device_auth_id",
                        code="device_code_incomplete")
    try:
        interval = max(3, int(data.get("interval", 5)))
    except (TypeError, ValueError):
        interval = 5

    return DeviceAuth(
        device_auth_id=device_auth_id,
        user_code=user_code,
        interval=interval,
        expires_at=time.monotonic() + DEVICE_LOGIN_TIMEOUT_SECONDS,
    )


def poll_device_login(auth: DeviceAuth, *, timeout: float = 15.0) -> dict[str, str] | None:
    """One poll. Returns tokens once approved, None while still pending.

    403/404 is the backend's "not approved yet" — it is the expected steady
    state of this loop, not an error.
    """
    resp = http_post(DEVICE_TOKEN_URL, json_body={
        "device_auth_id": auth.device_auth_id,
        "user_code": auth.user_code,
    }, timeout=timeout)

    if resp.status in {403, 404}:
        return None
    if resp.status != 200:
        raise AuthError(f"device auth polling returned status {resp.status}",
                        code="device_code_poll_failed")

    data = resp.json()
    if not isinstance(data, dict):
        raise AuthError("device auth response was not JSON", code="device_code_invalid")

    authorization_code = str(data.get("authorization_code") or "").strip()
    code_verifier = str(data.get("code_verifier") or "").strip()
    if not authorization_code or not code_verifier:
        raise AuthError("device auth response was missing authorization_code/code_verifier",
                        code="device_code_incomplete")

    return exchange_device_code(authorization_code, code_verifier, timeout=timeout)


def exchange_device_code(authorization_code: str, code_verifier: str, *,
                         timeout: float = 15.0) -> dict[str, str]:
    """Trade an approved authorization code for tokens."""
    resp = http_post(TOKEN_URL, form={
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
    }, timeout=timeout)

    if resp.status == 429:
        retry = _retry_after_seconds(resp.headers)
        hint = f" Try again in about {retry}s." if retry is not None else " Wait a minute and retry."
        raise AuthError(f"OpenAI is rate-limiting token exchange (429).{hint}",
                        code="rate_limited")
    if resp.status != 200:
        raise AuthError(f"token exchange returned status {resp.status}",
                        code="token_exchange_failed")

    payload = resp.json()
    if not isinstance(payload, dict):
        raise AuthError("token exchange returned invalid JSON", code="token_exchange_invalid")

    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise AuthError("token exchange did not return an access_token",
                        code="token_exchange_no_access_token")
    return {
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or "").strip(),
        "last_refresh": _utc_now(),
    }


def wait_for_tokens(auth: DeviceAuth, *, sleep=None,
                    timeout: float = 15.0) -> dict[str, str]:
    """Block until the user approves the device code. Used by the CLI."""
    sleep = sleep or time.sleep
    while not auth.expired():
        sleep(auth.interval)
        tokens = poll_device_login(auth, timeout=timeout)
        if tokens:
            return tokens
    raise AuthError("login timed out — the device code expired",
                    code="device_code_timeout")


# --------------------------------------------------------------------------
# The entry point the server actually calls
# --------------------------------------------------------------------------

def resolve_credentials(*, force_refresh: bool = False,
                        refresh_if_expiring: bool = True) -> dict[str, Any]:
    """Return usable credentials, refreshing and persisting when needed.

    Held under the store lock across read-refresh-write so two concurrent
    requests cannot both consume the same single-use refresh token.
    """
    with _store_lock:
        data = read_tokens()
        tokens = dict(data["tokens"])
        access = str(tokens.get("access_token") or "").strip()

        should_refresh = force_refresh or (
            refresh_if_expiring and access_token_is_expiring(access)
        )
        if should_refresh:
            refreshed = refresh_tokens(tokens.get("refresh_token", ""))
            tokens.update(refreshed)
            save_tokens(tokens, last_refresh=refreshed["last_refresh"])
            access = refreshed["access_token"]
            data = {"last_refresh": refreshed["last_refresh"]}

    return {
        "provider": PROVIDER_ID,
        "base_url": base_url(),
        "api_key": access,
        "account_id": account_id_from_token(access),
        "last_refresh": data.get("last_refresh"),
        "auth_mode": "chatgpt",
    }


def credential_status() -> dict[str, Any]:
    """Describe stored credentials without raising. Safe to expose — it reports
    on the token, it never returns it."""
    try:
        data = read_tokens()
    except AuthError as exc:
        return {"authenticated": False, "code": exc.code, "detail": str(exc)}

    access = str(data["tokens"].get("access_token") or "")
    claims = decode_jwt_claims(access)
    exp = claims.get("exp")
    return {
        "authenticated": True,
        "account_id": account_id_from_token(access),
        "expires_at": (datetime.fromtimestamp(float(exp), UTC).isoformat()
                       if isinstance(exp, (int, float)) else None),
        "expiring_soon": access_token_is_expiring(access),
        "can_refresh": bool(str(data["tokens"].get("refresh_token") or "").strip()),
        "last_refresh": data.get("last_refresh"),
        "base_url": base_url(),
        "store": str(store_path()),
    }
