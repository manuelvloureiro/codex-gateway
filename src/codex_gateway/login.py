"""Sign in to a ChatGPT Plus/Pro subscription from the command line.

    docker compose run --rm codex-gateway python -m codex_gateway.login
    docker compose run --rm codex-gateway python -m codex_gateway.login --import
    docker compose run --rm codex-gateway python -m codex_gateway.login --status

Prints a URL and a code; you approve in a browser on any machine. Tokens land
in $CODEX_GATEWAY_HOME/auth.json (the /data volume) and refresh automatically,
so this is a one-time step per volume.

The same flow is available over HTTP — see /auth/login/start in server.py — for
when there is no shell handy.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import oauth
from .oauth import AuthError

BLUE = "\033[94m"
RESET = "\033[0m"


def _print_status() -> int:
    status = oauth.credential_status()
    print(json.dumps(status, indent=2))
    return 0 if status.get("authenticated") else 1


def _do_import() -> int:
    tokens = oauth.import_codex_cli_tokens()
    if not tokens:
        print(f"no usable tokens in the Codex CLI store ({oauth.codex_cli_auth_path()})",
              file=sys.stderr)
        return 1
    path = oauth.save_tokens(tokens, label="imported-from-codex-cli")
    print(f"imported Codex CLI tokens into {path}")
    return 0


def _do_login() -> int:
    print("Starting ChatGPT device-code sign-in.\n")
    auth = oauth.start_device_login()

    print("To continue, follow these steps:\n")
    print("  1. Open this URL in your browser:")
    print(f"     {BLUE}{auth.verification_uri}{RESET}\n")
    print("  2. Enter this code:")
    print(f"     {BLUE}{auth.user_code}{RESET}\n")
    print("Waiting for sign-in... (press Ctrl+C to cancel)")

    tokens = oauth.wait_for_tokens(auth)
    path = oauth.save_tokens(tokens, label="device-code-login")
    print(f"\nsigned in — tokens saved to {path}")

    account = oauth.account_id_from_token(tokens["access_token"])
    if account:
        print(f"ChatGPT account: {account}")
    if not tokens.get("refresh_token"):
        print("warning: no refresh_token was issued — this credential cannot be "
              "renewed and will need a fresh login when it expires", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-gateway-login",
        description="ChatGPT subscription login for the codex-gateway service",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--import", dest="do_import", action="store_true",
                       help="import tokens from an existing Codex CLI install "
                            "($CODEX_HOME or ~/.codex/auth.json) instead of a new login")
    group.add_argument("--status", action="store_true",
                       help="show current credential status and exit")
    group.add_argument("--logout", action="store_true",
                       help="forget stored credentials")
    args = parser.parse_args(argv)

    try:
        if args.status:
            return _print_status()
        if args.logout:
            print("credentials cleared" if oauth.clear_tokens() else "nothing stored")
            return 0
        if args.do_import:
            return _do_import()
        return _do_login()
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130
    except AuthError as exc:
        print(f"\nlogin failed [{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
