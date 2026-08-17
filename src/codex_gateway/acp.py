"""Launch Codex's ACP adapter with this service as its model provider.

ACP is a stdio protocol between an editor and a coding *agent*.  The gateway
itself is only a model/authentication service, so the agent side is delegated
to the maintained ``@agentclientprotocol/codex-acp`` adapter.  That adapter
owns Codex sessions, tools, approvals, and sandboxing; this module only injects
the custom Responses provider configuration and then replaces itself with the
adapter process.

Replacing the process is important: stdout belongs exclusively to ACP's
newline-delimited JSON-RPC stream, and signals/cancellation must reach the
adapter directly.  Windows has no process replacement.  There the launcher
starts the adapter as a child process and waits for it; see ``run_windows``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

ADAPTER_PACKAGE = "@agentclientprotocol/codex-acp@1.1.9"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8085/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
PROVIDER_ID = "codex-gateway"

# An exact npm package spec, with no character a command interpreter reads.  On
# Windows `npx` is `npx.cmd`.  CreateProcess starts a command file with cmd.exe,
# and cmd.exe reads the arguments a second time.  An exact name and version
# carries nothing for it to act on.
PACKAGE_SPEC = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9.+_-]*)?$"
)


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


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise AcpConfigError(
            "node was not found; install Node.js 20+ and make sure it is on PATH"
        )
    return node


def adapter_command(environ: Mapping[str, str]) -> list[str]:
    """Use an explicit adapter executable or the pinned npx package.

    ``CODEX_ACP_BIN`` can also name the adapter's ``dist/index.js`` file, which
    is started with ``node``.  On Windows that file is the only direct target:
    the package contains no program of its own, and ``npm install -g`` installs
    a ``codex-acp.cmd`` file instead.
    """
    override = (environ.get("CODEX_ACP_BIN") or "").strip()
    if override:
        if override.lower().endswith(".js"):
            return [_node(), override]
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
    if not PACKAGE_SPEC.match(package):
        raise AcpConfigError(
            f"CODEX_ACP_PACKAGE must be an exact `name@version` spec (got {package!r})"
        )
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


def kill_on_close_job() -> Any:
    """Return a function that adds a process to a job object tied to this one.

    ACP clients stop the launcher with ``TerminateProcess``, and no program can
    intercept that call.  Without a job object, the adapter and the Codex
    app-server below it continue to run and hold the pipes open, so a client
    that restarts the agent leaves one more Codex process each time.  With the
    ``KILL_ON_JOB_CLOSE`` limit, the kernel stops the members when the last
    handle closes.  This process holds that handle and never closes it.

    Raises ``OSError`` if the job object cannot be created.  The caller then
    prints a warning and continues.
    """
    import ctypes
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)

    def assign(process: subprocess.Popen) -> None:
        if not kernel32.AssignProcessToJobObject(handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    return assign


def run_windows(
    command: list[str],
    environ: Mapping[str, str],
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    job: Callable[[], Any] = kill_on_close_job,
) -> int:
    """Run the adapter as a child process and return its exit code.

    On Windows, ``os.execvpe`` is a CRT emulation.  It starts a new process and
    stops this one immediately.  An ACP client reads that as an agent that
    stops one second after it connects, so the launcher waits here instead.
    The adapter keeps the three standard handles, so it writes to the ACP
    stdout directly.  This process copies nothing.
    """
    try:
        assign = job()
    except OSError as exc:
        assign = None
        print(
            f"codex-gateway-acp: cannot isolate the adapter in a job object ({exc}); "
            "it may outlive this launcher",
            file=sys.stderr,
        )

    process = popen(command, env=dict(environ))
    if assign is not None:
        try:
            assign(process)
        except OSError as exc:
            print(
                f"codex-gateway-acp: cannot assign the adapter to a job object ({exc}); "
                "it may outlive this launcher",
                file=sys.stderr,
            )
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The console sent Ctrl+C to the full process group, and the
            # adapter handles it. Continue to wait for its exit code.
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-gateway-acp",
        description="run Codex over ACP with codex-gateway as its model provider",
    )
    parser.parse_args(argv)

    try:
        command = adapter_command(os.environ)
        environ = adapter_environment(os.environ)
        if os.name == "nt":
            return run_windows(command, environ)
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
