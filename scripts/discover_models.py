#!/usr/bin/env python3
"""Print the model catalogue this subscription actually serves.

`/models` is a static list from ``CODEX_MODELS``, so it only ever reflects what
someone typed.  The authoritative answer lives in the Codex app-server, which
the CLI already queries to build its own picker: ``model/list`` over stdio
JSON-RPC.  Asking it is exact, costs no model tokens, and needs no credential
handling here — the CLI's existing session does the authenticating.

    python3 scripts/discover_models.py              # table
    python3 scripts/discover_models.py --env        # CODEX_MODELS=... line
    python3 scripts/discover_models.py --json       # full records

Requires the `codex` CLI on PATH and a completed `codex login`.  Hidden models
(`codex-auto-review`, for one) are excluded unless --include-hidden is given:
they are real and callable, but they are not offered to users by design.
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading

TIMEOUT_SECONDS = 90


def _read_catalogue(include_hidden: bool) -> list[dict]:
    """Drive `codex app-server` far enough to answer model/list."""
    proc = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)
    if proc.stdin is None or proc.stdout is None:      # pragma: no cover
        raise RuntimeError("codex app-server exposed no stdio pipes")

    inbox: queue.Queue[tuple[str, str]] = queue.Queue()

    def pump(stream, tag):
        for line in stream:
            inbox.put((tag, line.rstrip()))

    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=pump, args=(stream, tag), daemon=True).start()

    for request in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "codex-gateway", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "model/list",
         "params": {"includeHidden": include_hidden, "limit": 100}},
    ):
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

    try:
        remaining = TIMEOUT_SECONDS
        while remaining > 0:
            try:
                tag, line = inbox.get(timeout=3)
            except queue.Empty:
                remaining -= 3
                continue
            if tag == "err":
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != 2:
                continue
            if "error" in message:
                raise RuntimeError(f"model/list failed: {message['error']}")
            return (message.get("result") or {}).get("data") or []
        raise RuntimeError(f"no model/list answer within {TIMEOUT_SECONDS}s")
    finally:
        proc.kill()


def _efforts(model: dict) -> list[str]:
    """Reasoning efforts as plain strings.

    The catalogue returns objects with a description; the wire wants the bare
    name.  `ultra` appears here but is an app-server behaviour (it delegates
    tasks) rather than a value `/responses` accepts, so it is dropped.
    """
    names = []
    for item in model.get("supportedReasoningEfforts") or []:
        name = item if isinstance(item, str) else item.get("reasoningEffort")
        if name and name != "ultra":
            names.append(name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", action="store_true",
                        help="print a CODEX_MODELS= line for .env")
    parser.add_argument("--json", action="store_true",
                        help="print the full records as JSON")
    parser.add_argument("--include-hidden", action="store_true",
                        help="include models hidden from the picker")
    args = parser.parse_args()

    try:
        models = _read_catalogue(args.include_hidden)
    except (OSError, RuntimeError) as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 1

    if not args.include_hidden:
        models = [m for m in models if not m.get("hidden")]
    if not models:
        print("no models returned", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(models, indent=1))
        return 0
    if args.env:
        print("CODEX_MODELS=" + ",".join(m.get("id", "") for m in models))
        return 0

    width = max(len(m.get("id", "")) for m in models)
    for model in models:
        marker = "*" if model.get("isDefault") else " "
        print(f"{marker} {model.get('id',''):<{width}}  "
              f"{','.join(_efforts(model)) or '-':<28}  "
              f"{model.get('displayName','')}")
    print("\n* default. Reasoning efforts are the values /responses accepts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
