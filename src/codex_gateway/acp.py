"""Launch Codex's ACP adapter with this service as its model provider.

ACP is a stdio protocol between an editor and a coding *agent*.  The gateway
itself is only a model/authentication service, so the agent side is delegated
to the maintained ``@agentclientprotocol/codex-acp`` adapter.  That adapter
owns Codex sessions, tools, approvals, and sandboxing; this module only injects
the custom Responses provider configuration and then replaces itself with the
adapter process.

Replacing the process is important: stdout belongs exclusively to ACP's
newline-delimited JSON-RPC stream, and signals/cancellation must reach the
adapter directly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit

ADAPTER_PACKAGE = "@agentclientprotocol/codex-acp@1.1.9"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8085/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
PROVIDER_ID = "codex-gateway"


class AcpConfigError(ValueError):
    """Configuration that cannot safely be handed to the ACP adapter."""


def _gateway_url(environ: Mapping[str, str]) -> str:
    value = (
        (environ.get("CODEX_GATEWAY_URL") or DEFAULT_GATEWAY_URL).strip().rstrip("/")
    )
    try:
        parsed = urlsplit(value)
        # Accessing these properties performs validation that urlsplit itself
        # defers, including malformed IPv6 brackets and out-of-range ports.
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise AcpConfigError(f"CODEX_GATEWAY_URL is invalid: {exc}") from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or any(character.isspace() for character in value)
    ):
        raise AcpConfigError(
            f"CODEX_GATEWAY_URL must be an absolute http(s) URL (got {value!r})"
        )
    if parsed.username is not None or parsed.password is not None:
        raise AcpConfigError(
            "CODEX_GATEWAY_URL must not contain credentials; use a trusted "
            "network path to the keyless provider"
        )
    if parsed.query or parsed.fragment:
        raise AcpConfigError("CODEX_GATEWAY_URL must not contain a query or fragment")
    return value


def _model(environ: Mapping[str, str]) -> str:
    explicit = (environ.get("CODEX_GATEWAY_MODEL") or "").strip()
    if explicit:
        return explicit
    configured = [
        item.strip()
        for item in (environ.get("CODEX_MODELS") or "").split(",")
        if item.strip()
    ]
    return configured[0] if configured else DEFAULT_MODEL


def codex_config(environ: Mapping[str, str]) -> dict[str, Any]:
    """Merge the gateway provider into an optional caller-supplied CODEX_CONFIG.

    Unknown top-level settings and provider-specific tuning are retained.  The
    fields that define this provider are authoritative so a stale user setting
    cannot silently route the ACP session around the gateway or enable the
    WebSocket transport that the HTTP service does not expose.
    """
    raw = (environ.get("CODEX_CONFIG") or "").strip()
    if raw:
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AcpConfigError(
                f"CODEX_CONFIG is not valid JSON: {exc.msg} at character {exc.pos}"
            ) from exc
        if not isinstance(config, dict):
            raise AcpConfigError("CODEX_CONFIG must be a JSON object")
    else:
        config = {}

    providers = config.get("model_providers")
    if providers is None:
        providers = {}
    elif not isinstance(providers, dict):
        raise AcpConfigError("CODEX_CONFIG.model_providers must be a JSON object")
    else:
        providers = dict(providers)

    existing = providers.get(PROVIDER_ID)
    if existing is None:
        provider: dict[str, Any] = {}
    elif isinstance(existing, dict):
        provider = dict(existing)
    else:
        raise AcpConfigError(
            f"CODEX_CONFIG.model_providers.{PROVIDER_ID} must be a JSON object"
        )

    provider.update(
        {
            "name": "Codex Gateway",
            "base_url": _gateway_url(environ),
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
        }
    )
    providers[PROVIDER_ID] = provider

    merged = dict(config)
    merged["model"] = _model(environ)
    merged["model_provider"] = PROVIDER_ID
    merged["model_providers"] = providers
    return merged


def adapter_command(environ: Mapping[str, str]) -> list[str]:
    """Use an explicit adapter executable or the pinned npx package."""
    override = (environ.get("CODEX_ACP_BIN") or "").strip()
    if override:
        return [override]

    npx = shutil.which("npx")
    if not npx:
        raise AcpConfigError(
            "npx was not found; install Node.js 20+, or install "
            f"`{ADAPTER_PACKAGE}` and set CODEX_ACP_BIN to its executable"
        )
    package = (environ.get("CODEX_ACP_PACKAGE") or ADAPTER_PACKAGE).strip()
    if not package:
        raise AcpConfigError("CODEX_ACP_PACKAGE cannot be empty")
    return [npx, "--yes", package]


def adapter_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the environment consumed by the maintained Codex ACP adapter."""
    result = dict(environ)
    gateway_url = _gateway_url(environ)
    result["CODEX_CONFIG"] = json.dumps(
        codex_config(environ), separators=(",", ":"), sort_keys=True
    )
    result["MODEL_PROVIDER"] = PROVIDER_ID
    # account/read happens before a Codex session (and its CODEX_CONFIG
    # overrides) exists.  A fresh Codex home may therefore report that OpenAI
    # auth is required.  The adapter's supported default-auth hook selects its
    # custom-gateway provider in that case, without depending on a client-side
    # implementation of the optional gateway-auth capability.
    result["DEFAULT_AUTH_REQUEST"] = json.dumps(
        {
            "methodId": "gateway",
            "_meta": {
                "gateway": {
                    "baseUrl": gateway_url,
                    "headers": {},
                    "providerName": "Codex Gateway",
                },
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    # Authentication belongs to codex-gateway.  Hiding the adapter's browser
    # login prevents users from accidentally creating a second, unrelated
    # Codex credential flow in the editor.
    result["NO_BROWSER"] = "1"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-gateway-acp",
        description="run Codex over ACP with codex-gateway as its model provider",
    )
    parser.parse_args(argv)

    try:
        command = adapter_command(os.environ)
        environ = adapter_environment(os.environ)
        os.execvpe(command[0], command, environ)
    except AcpConfigError as exc:
        print(f"codex-gateway-acp: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"codex-gateway-acp: cannot start ACP adapter: {exc}", file=sys.stderr)
        return 127
    return 0  # pragma: no cover - a successful exec never returns


if __name__ == "__main__":
    raise SystemExit(main())
