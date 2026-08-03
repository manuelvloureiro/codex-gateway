# codex-gateway

Serves a ChatGPT Plus/Pro subscription as a keyless, OpenAI-compatible
provider, so any client that speaks `chat/completions` can use it. An optional
ACP launcher also makes the provider available as a full Codex coding agent in
ACP-capable editors such as VS Code.

The HTTP service is self-contained: one runtime dependency, `aiohttp`. The
optional ACP launcher uses the external Codex ACP adapter described below.

## Why it exists

ChatGPT's Codex backend is *not* an OpenAI-compatible API. Every rule below was
established by making the request and reading the rejection:

- `/responses` only — there is no `/chat/completions`
- `input` must be a list, `stream` must be true, `store` must be false
- `originator: codex_cli_rs` plus a matching User-Agent, or the backend serves
  a restricted surface
- `ChatGPT-Account-ID`, decoded from the access token's JWT payload
- `/models` needs a `client_version` param and returns `[]` anyway, so the
  catalogue is served statically

Model names are not the public ones: `gpt-5.6-sol` works, bare `gpt-5.6` is
rejected. Check `~/.codex/config.toml` for what your CLI actually uses.

## Layout

```
src/codex_gateway/
  oauth.py      device-code login, token store, refresh   (no I/O beyond HTTP)
  translate.py  chat/completions <-> Responses            (pure functions)
  server.py     aiohttp app: provider routes + /auth/*
  login.py      CLI front end
  acp.py        stdio launcher for the maintained Codex ACP adapter
app/index.html  reference UI (login/logout/test a message), served at /
tests/          unit tests, no network, no real credential store
vscode-extension/ repository-owned VS Code ACP client, tests, and VSIX source
```

`oauth` is synchronous so the CLI can call it directly; `server` wraps it in
`asyncio.to_thread` so a blocking refresh never stalls a live stream.

## Signing in

Two front ends over the same flow. Tokens land in `$CODEX_GATEWAY_HOME/auth.json`
(the `/data` volume) and refresh automatically, so this is one step per volume.

**CLI**

```bash
docker compose run --rm codex-gateway python -m codex_gateway.login
docker compose run --rm codex-gateway python -m codex_gateway.login --import   # from Codex CLI
docker compose run --rm codex-gateway python -m codex_gateway.login --status
docker compose run --rm codex-gateway python -m codex_gateway.login --logout
```

**Browser** — start the service and open <http://localhost:8085/>. Everything
runs on 8085, container and host alike. The page at `app/index.html` does
login, logout, import/refresh, and sends a test message. It is a reference to
copy from when wiring this into a real front end, not a product.

**API** — two calls, which is all the UI does:

```bash
curl -XPOST localhost:8085/auth/login/start
# -> {"device_auth_id": "...", "user_code": "ABCD-1234",
#     "verification_uri": "https://auth.openai.com/codex/device", "interval": 5}
# approve the code in a browser, then poll every `interval` seconds:
curl -XPOST localhost:8085/auth/login/poll -H 'Content-Type: application/json' \
     -d '{"device_auth_id": "..."}'
# -> {"status": "pending"}  ... then {"status": "complete", ...}
```

## API

Every route. `tests/test_server.py::TestRouting::test_readme_documents_every_route`
fails if this table drifts from what the app registers.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/` | — | Reference UI (`app/index.html`). |
| `GET` | `/health` | — | `200` when signed in, `503` when not. Body is the credential status. |
| `GET` | `/models` | — | Static catalogue from `CODEX_MODELS`. |
| `POST` | `/responses` | — | Proxied upstream unchanged, bar the forced invariants. Streams SSE. |
| `POST` | `/chat/completions` | — | Translated to/from `/responses`. Honours `stream`. |
| `GET` | `/auth/status` | admin | Credential state. Never returns the token itself. |
| `POST` | `/auth/login/start` | admin | Begin device-code login. Returns the code and URL. |
| `POST` | `/auth/login/poll` | admin | `{"device_auth_id"}` → `pending` or `complete`. Saves tokens on completion. |
| `POST` | `/auth/import` | admin | Adopt the local Codex CLI's tokens. |
| `POST` | `/auth/refresh` | admin | Force a token refresh. |
| `POST` | `/auth/logout` | admin | Forget stored credentials. |

`/models`, `/responses` and `/chat/completions` are also served under `/v1`, for
clients that keep the prefix.

**Auth** — "admin" means the route requires `Authorization: Bearer <token>`
*only* when `CODEX_ADMIN_TOKEN` is set; otherwise every route is open. The
provider surface can never require it (see below).

**Errors** are uniform: `{"error": {"message": ..., "code": ...}}`. `401` means
no usable credential, `429` means upstream quota (not a credential problem, so
do not re-login), `403` means the admin token is missing or wrong.

## Registering as a provider

The gateway holds the credential, so register it keyless. For Bifrost:

```
base_provider_type    = "openai"
is_key_less           = true
base_url              = http://codex-gateway:8085
allow_private_network = true
```

## ACP in VS Code

ACP is an agent-to-editor protocol, not another HTTP endpoint. This repository
now includes both sides needed by VS Code:

- `codex-gateway-acp`, the agent-side stdio launcher
- `vscode-extension/`, a repository-owned ACP client built directly on the
  official VS Code Extension API and Node.js built-ins

The editor integration does **not** install or load a third-party Marketplace
ACP client. Package it locally as a VSIX and review the complete client source
in this repository.

```text
VS Code Extension Host
  <-- official VS Code API --> in-repo Webview View
  <-- stdio ACP v1 ---------> codex-gateway-acp
  <-- Codex app-server -----> /v1/responses on codex-gateway
```

The in-repo client implements newline-delimited JSON-RPC, session and prompt
lifecycle, streamed messages, plans and tool calls, cancellation, configuration
selectors, multi-root workspaces, and permission requests. Permissions are
shown in trusted VS Code UI rather than inside the webview. Codex App Server
supplies the coding-agent behavior; `codex-gateway` continues to own the
ChatGPT credential, refresh, and model traffic.

### 1. Prepare the gateway and launcher

ACP uses the same already-signed-in gateway as every other client. Start it,
sign in at <http://localhost:8085/> if needed, and verify that health is `200`:

```bash
docker compose up -d codex-gateway
curl -f http://127.0.0.1:8085/health
```

Install the Python entry point and ensure VS Code 1.100 or newer and Node.js 20
or newer are available:

```bash
uv sync --frozen
code --version
node --version
```

VS Code 1.100+ is required by the in-repo extension. Node.js 20+ is the tested
setup for the launcher, tests, and official VSIX packaging tool.

The launcher uses the maintained agent-side
[Codex ACP adapter](https://github.com/agentclientprotocol/codex-acp) to connect
ACP to Codex App Server. This is separate from the VS Code client. By default,
the launcher runs its pinned version through `npx`; it never silently selects
an arbitrary global binary. To avoid a first-connection download, preinstall
the same version and configure its absolute path later:

```bash
npm install -g @agentclientprotocol/codex-acp@1.1.9
command -v codex-acp
codex-acp --version
```

The two equivalent launcher commands are:

```bash
.venv/bin/codex-gateway-acp
.venv/bin/codex-gateway acp
```

They are stdio servers and will wait silently for an ACP client when started
by hand. Human-readable diagnostics go to stderr; stdout is reserved for ACP
JSON-RPC.

### 2. Package and install the in-repo VS Code client

Run the client tests, package it with Microsoft's pinned `@vscode/vsce`, and
install the resulting local VSIX:

```bash
cd vscode-extension
npm test
npm run package
code --install-extension ./codex-gateway-acp-vscode.vsix --force
```

Reload VS Code after installation. A locally installed VSIX does not update
automatically; rebuild and reinstall it after changing `vscode-extension/`.
The extension has no runtime npm dependencies. `npm run package` may download
the pinned official Microsoft packaging tool on first use.

For workspaces outside this checkout, add the absolute launcher path to VS
Code's user `settings.json`. The remaining values below are secure defaults:

```json
{
  "codexGateway.launcherPath": "/absolute/path/to/codex-gateway/.venv/bin/codex-gateway-acp",
  "codexGateway.gatewayUrl": "http://127.0.0.1:8085/v1",
  "codexGateway.model": "gpt-5.6-sol",
  "codexGateway.initialMode": "read-only",
  "codexGateway.permissionPolicy": "ask",
  "codexGateway.logProtocol": false
}
```

If the adapter was preinstalled, also set `codexGateway.adapterPath` to the
absolute path printed by `command -v codex-acp`. Otherwise the launcher uses
its pinned `npx` package.

### 3. Work with it

1. Open and trust a local filesystem workspace.
2. Open **Codex Gateway** in the Activity Bar.
3. Send a task. The client lazily starts the ACP process and creates a session.
4. Use the selectors above the transcript to change mode, collaboration mode,
   model, reasoning effort, or other options published by the agent.
5. Use the view toolbar to start a fresh session, cancel a turn, or restart and
   immediately reconnect the agent. **Codex Gateway: Show ACP Logs** opens
   diagnostics.

The first file-scheme workspace folder becomes `cwd` by default. Every other
folder in a multi-root workspace is passed as an ACP additional directory when
supported. If `codexGateway.workingDirectory` selects a different folder, all
file-scheme workspace roots except that exact directory are passed as additions.
The default **Read-only** mode asks before edits and commands. **Agent** allows
ordinary workspace edits and sandboxed commands without prompting every time.
**Agent Full Access** removes those safeguards and is only for deliberately
trusted work. Dismissing a permission picker cancels that permission request.

### How the launcher configures Codex

The launcher merges (rather than discards) an existing `CODEX_CONFIG`, then
forces a custom Responses provider with the selected gateway URL and model.
It disables Codex WebSocket transport because this service exposes SSE, and it
injects the adapter's supported gateway-auth request so a separate Codex login
is never required. Authentication still happens only in `codex-gateway`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODEX_GATEWAY_URL` | `http://127.0.0.1:8085/v1` | Gateway base URL visible from the ACP process. Codex appends `/responses`. |
| `CODEX_GATEWAY_MODEL` | first `CODEX_MODELS` entry, then `gpt-5.6-sol` | Initial Codex model. |
| `CODEX_ACP_BIN` | *(unset)* | Explicit adapter executable. If unset, the launcher uses pinned `npx`. |
| `CODEX_ACP_PACKAGE` | `@agentclientprotocol/codex-acp@1.1.9` | Package used by `npx`. Override only to test a deliberate adapter version. |
| `CODEX_CONFIG` | `{}` | Extra Codex JSON configuration to merge before gateway fields are enforced. |
| `INITIAL_AGENT_MODE` | adapter default | `read-only`, `agent`, or `agent-full-access`. Start with `read-only`. |

Without `CODEX_ACP_BIN`, the first launch may download and execute the pinned
npm package and its dependencies. Preinstall it and set the absolute binary
path for offline or tightly controlled environments.

### Remote and container setups

- Keep the ACP launcher on the same side of an SSH/WSL/Dev Container boundary
  as the workspace and toolchain. `extensionKind: workspace` makes VS Code run
  this extension on that side as well. The HTTP gateway can remain in Docker.
- `127.0.0.1` is resolved where the ACP process runs. If the launcher is inside
  a Dev Container, it refers to that container, not the host. Put the launcher
  and gateway on a shared Docker network and use a service URL such as
  `http://codex-gateway:8085/v1`, or use a correctly configured host address.
  Do not broaden the keyless gateway's bind address beyond a trusted network.
- With VS Code Remote SSH, install/configure the extension in the remote
  context and use paths and a gateway URL that are valid on that remote host.
- If the gateway runs on another machine, keep its port on loopback and use an
  SSH tunnel such as `ssh -N -L 8085:localhost:8085 user@remote-host`; the ACP
  URL remains `http://127.0.0.1:8085/v1`.
- `CODEX_ADMIN_TOKEN` does not protect model use. Keep the provider port on a
  trusted interface exactly as described in the security section below.
- Prompts, selected workspace context, tool results, and permitted command/file
  operations pass through Codex App Server to the remote ChatGPT/Codex backend.
  The local extension does not make the model interaction offline or private.
- The launcher is currently supported on Linux and macOS. On Windows, run the
  extension and launcher in WSL or a Dev Container so the POSIX entry point and
  workspace paths are used.

### ACP troubleshooting

- **Gateway health is 503:** finish the gateway's device-code login; the ACP
  adapter deliberately has no separate ChatGPT credential.
- **No Codex Gateway icon:** install the generated VSIX, reload VS Code, and
  open a trusted local workspace. Web-only and untrusted workspaces are
  intentionally unsupported.
- **`codex-acp` or `npx` not found:** install Node.js 20+ and the pinned npm
  package, then set `codexGateway.adapterPath` to its absolute executable path.
- **The launcher cannot be found:** set `codexGateway.launcherPath` to the
  absolute `.venv/bin/codex-gateway-acp` path on the extension host.
- **404 for `/responses`:** `codexGateway.gatewayUrl` must be a base under
  which the gateway serves `/responses`. The recommended `/v1` base produces
  `/v1/responses`; the gateway also supports the bare base.
- **The editor asks for a Codex login:** make sure it launches
  `codex-gateway-acp`, not the upstream adapter directly. The wrapper injects
  the keyless gateway authentication configuration.
- **The connection exits immediately:** run **Codex Gateway: Show ACP Logs**
  from the Command Palette. Adapter/Codex stderr is always captured. Enable
  `codexGateway.logProtocol` only temporarily if complete ACP frames are needed.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODEX_GATEWAY_HOME` | `/data` | Where `auth.json` lives. |
| `CODEX_GATEWAY_PORT` | `8085` | Listen port. `--port` on the command line wins. |
| `CODEX_ADMIN_TOKEN` | *(unset)* | When set, `/auth/*` requires `Authorization: Bearer <token>`. Guards sign-in/out only — see below. |
| `CODEX_MODELS` | `gpt-5.6-sol,gpt-5.4` | Static `/models` catalogue. |
| `CODEX_BASE_URL` | ChatGPT backend | Upstream override. |
| `CODEX_UI` | `1` | `0` stops serving the reference page at `/`. |
| `CODEX_UI_DIR` | `./app` | Where to read `index.html` from. |
| `CODEX_CLI_VERSION` | `0.146.0` | Version pinned in the `codex_cli_rs` User-Agent. |
| `LOG_LEVEL` | `INFO` | |

## Behaviour worth knowing

- **It starts unauthenticated on purpose.** Exiting would make the HTTP login
  unreachable. `/health` returns 503 and completions return 401 until you sign
  in.
- **A 401 from upstream triggers one forced refresh and retry.** Another client
  (Codex CLI, a second gateway) can rotate the shared refresh token out from
  under this one.
- **429 is passed through as 429, never 401.** Quota exhaustion is not a
  credential problem, and a re-login cannot lift a rate limit.
- **Refresh tokens are single-use.** Rotation is persisted under a lock, so
  concurrent requests cannot both consume the same token.

## Development

```bash
uv sync
uv run pytest
(cd vscode-extension && npm test)
uv run python -m codex_gateway                   # serve on :8085
uv run python -m codex_gateway --port 9000       # or pick a port
uv run python -m codex_gateway.login --status
```

Tests never touch the network or a real credential store — `conftest.py`
enforces both with autouse fixtures.

## Credits

The OAuth implementation in `src/codex_gateway/oauth.py` — the device-code
flow, the refresh semantics, and the on-disk store format — follows the
[Hermes CLI](https://github.com/NousResearch/hermes-agent) (MIT, © 2025 Nous
Research). It is reimplemented here rather than imported, so this service
carries no dependency on it.

## Reaching it from another machine

The port publishes to the host's loopback by default, so it is **not** visible
on the VPN address until you say so. `CODEX_GATEWAY_BIND` picks the host
interface.

**SSH tunnel** — nothing to configure, and nothing new is exposed:

```bash
ssh -N -L 8085:localhost:8085 user@remote-host
```

Leave it running and open <http://localhost:8085/>. The browser is on your
machine, so the ChatGPT approval page works normally.

**Private interface** — on a VPN such as WireGuard, bind that interface's
address instead of loopback:

```bash
# .env — the address of this host on the VPN, not the public one
CODEX_GATEWAY_BIND=192.0.2.10
```

```bash
docker compose up -d codex-gateway
```

Then open `http://192.0.2.10:8085/` from a peer. Two things to check:

- **The bind moves the port, it does not add one.** `127.0.0.1:8085` stops
  answering on the gateway host once you do this. Containers on the same
  compose network keep reaching it as `http://codex-gateway:8085` either way.
- **The firewall must allow the port on that interface**, scoped to the VPN
  subnet and no wider — for example
  `ufw allow in on wg0 from 192.0.2.0/24 to any port 8085 proto tcp`. Confirm
  the public interface still refuses the port afterwards:

  ```bash
  curl -sS -m 5 http://<public-address>:8085/health   # must fail to connect
  ```

Set `CODEX_ADMIN_TOKEN` whenever you do this. Anyone who can reach the port can
spend the subscription, so keep the subnet one you trust.

### Driving it from VS Code on another machine

The extension never speaks HTTP to the gateway. It spawns `codex-gateway-acp`
as a child process, and *that* process makes the request. So the launcher, Node,
and this checkout all have to exist on the machine holding your code — only
model traffic crosses the network. `extensionKind: workspace` pins the
extension to that same side.

1. **Reach the gateway.** Either bind a private interface as above, or leave it
   on loopback and tunnel. Confirm before going further; nothing below works
   until this does:

   ```bash
   curl -sf http://<gateway-host>:8085/health && echo OK
   ```

2. **Check prerequisites:** Python 3.11+, Node.js 20+, VS Code 1.100+. On
   Windows use WSL or a Dev Container — the launcher is a POSIX entry point.

3. **Build the launcher** on the machine with your code:

   ```bash
   git clone <this-repo> codex-gateway && cd codex-gateway
   uv sync --frozen        # or: python3 -m venv .venv && .venv/bin/pip install -e .
   .venv/bin/codex-gateway-acp --help
   ```

   Smoke-test with `--help`. Run bare, the launcher waits silently on stdin for
   an ACP client, which is easily mistaken for a hang.

4. **Build and install the extension**, then reload VS Code:

   ```bash
   cd vscode-extension
   npm test && npm run package
   code --install-extension ./codex-gateway-acp-vscode.vsix --force
   ```

5. **Point it at the gateway** in user `settings.json`:

   ```json
   {
     "codexGateway.launcherPath": "/absolute/path/to/codex-gateway/.venv/bin/codex-gateway-acp",
     "codexGateway.gatewayUrl": "http://<gateway-host>:8085/v1",
     "codexGateway.model": "gpt-5.6-sol",
     "codexGateway.initialMode": "read-only",
     "codexGateway.permissionPolicy": "ask"
   }
   ```

   `launcherPath` must be absolute and must name the local copy. Omit it only
   when the open workspace *is* this checkout: auto-discovery looks in
   `<workspace>/.venv/bin` and `<workspace>/services/models/codex-gateway/.venv/bin`
   and nowhere else. Keep the `/v1` — Codex appends `/responses` to it.

6. **Open a trusted local folder**, click **Codex Gateway** in the Activity Bar,
   and send a task. If nothing happens, run **Codex Gateway: Show ACP Logs**;
   adapter and Codex stderr are always captured there.

The agent runs on your machine and touches your files; the gateway host only
holds the ChatGPT credential and relays model traffic. Start in **Read-only**,
which asks before edits and commands.

### CODEX_ADMIN_TOKEN

Optional. When set, `/auth/*` requires `Authorization: Bearer <token>`; the
reference UI hides its token field until a request comes back 403.

**Set it** when the port is reachable by anyone you would not hand your ChatGPT
login to — a shared VPN subnet, a LAN, a routable interface. It stops a peer
from logging you out or hijacking a sign-in.

**Skip it** on loopback plus an SSH tunnel, on the internal compose network, or
on a subnet that is only you.

It guards *management*, not *use*. `/chat/completions` and `/responses` cannot
require a token — Bifrost has none to send — so anyone who can reach the port
can spend the subscription whether or not the admin token is set. For that,
reachability is the only control.

**No browser at all** — the CLI prints a code you approve from any device:

```bash
docker compose run --rm codex-gateway python -m codex_gateway.login
```
