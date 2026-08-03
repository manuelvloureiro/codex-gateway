"""Shared fixtures.

Two invariants for this suite:
  * no test touches the network — oauth.http_post is always stubbed
  * no test touches a real credential store — it is always a tmp_path
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from codex_gateway import oauth


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the credential store at a temp dir for every test.

    Autouse and unconditional: a bug that writes to the real ~/.codex or /data
    during a test run would be destroying live credentials.
    """
    monkeypatch.setenv("CODEX_GATEWAY_HOME", str(tmp_path / "store"))
    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli"))
    return tmp_path / "store"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test reaches http_post without stubbing it."""
    def _blocked(url, **kwargs):
        raise AssertionError(f"unstubbed network call to {url}")

    monkeypatch.setattr(oauth, "http_post", _blocked)


def make_jwt(*, exp: float | None = None, account_id: str | None = "acct-123") -> str:
    """Build an unsigned JWT with the claims the gateway reads.

    The signature is never verified by us (the backend does that), so a
    placeholder is enough and keeps the tests free of a crypto dependency.
    """
    claims: dict = {}
    if exp is not None:
        claims["exp"] = exp
    if account_id is not None:
        claims["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64(json.dumps({'alg': 'none'}).encode())}.{b64(json.dumps(claims).encode())}.sig"


@pytest.fixture
def fresh_token():
    return make_jwt(exp=time.time() + 3600)


@pytest.fixture
def expired_token():
    return make_jwt(exp=time.time() - 60)


@pytest.fixture
def stub_post(monkeypatch):
    """Queue HttpResponses for oauth.http_post and record the calls made."""
    calls: list[dict] = []
    queue: list = []

    def _post(url, *, json_body=None, form=None, timeout=15.0):
        calls.append({"url": url, "json": json_body, "form": form})
        if not queue:
            raise AssertionError(f"no queued response for {url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(oauth, "http_post", _post)
    return type("StubPost", (), {"calls": calls, "queue": queue})()


def response(status: int, payload=None, headers=None) -> oauth.HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode()
    return oauth.HttpResponse(status, body, headers or {})
