"""``python -m codex_gateway`` — run the gateway or its ACP launcher."""

from __future__ import annotations

import sys

from .server import run

USAGE = """usage: codex-gateway [--port PORT] | codex-gateway acp

Run the HTTP gateway by default, or run its stdio ACP launcher with `acp`.

  --port PORT   listen port (default: $CODEX_GATEWAY_PORT, else 8085)
"""


def _parse_port(args: list[str]) -> int | None:
    """Pull `--port N` / `--port=N` off the front, or raise ValueError."""
    if not args:
        return None
    if args[0] == "--port":
        if len(args) != 2:
            raise ValueError("--port takes exactly one value")
        raw = args[1]
    elif args[0].startswith("--port="):
        if len(args) != 1:
            raise ValueError("unexpected extra arguments")
        raw = args[0].split("=", 1)[1]
    else:
        raise ValueError(f"unrecognised argument: {args[0]}")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return port


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["-h"], ["--help"]):
        print(USAGE, end="")
        return 0
    if args and args[0] == "acp":
        from .acp import main as acp_main

        return acp_main(args[1:])
    try:
        port = _parse_port(args)
    except ValueError as exc:
        print(f"codex-gateway: {exc}", file=sys.stderr)
        print(USAGE.splitlines()[0], file=sys.stderr)
        return 2
    return run(port)


if __name__ == "__main__":
    raise SystemExit(main())
