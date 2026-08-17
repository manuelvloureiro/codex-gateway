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
| `GET` | `/ca.crt` | — | The certificate named by `CODEX_CA_FILE`, as a download. `404` when unset. |
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

**`/models` reflects `CODEX_MODELS`, not the account.** It is a list someone
typed, so it can silently omit models the subscription serves — and a client
picks from that list, so an omission makes the model unreachable. The account's
real catalogue comes from the Codex app-server, which is what the CLI's own
picker is built from:

```bash
python3 scripts/discover_models.py            # what this account serves
python3 scripts/discover_models.py --env      # a CODEX_MODELS= line for .env
```

It needs the `codex` CLI and a completed login, costs no model tokens, and
handles no credential of its own — the CLI's session does the authenticating.
Hidden models are excluded unless `--include-hidden` is passed; they are real
and callable, but deliberately not offered to users.

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

## VS Code Chat

Chat's model picker accepts a custom provider, so these models can sit beside
the built-in ones and drive ask, edit, and agent mode. For ordinary editor work
this is the route to use. It is not the ACP client further down: that one is a
separate agent panel and puts nothing in the picker.

**Chat view → model picker → Manage Models… → Custom Endpoint.** The dialog
insists on an API key; the gateway is keyless, so any non-empty string does.

VS Code stores the result in `chatLanguageModels.json`, and editing that file
is faster than the dialog once the provider exists:

| Windows | `%APPDATA%\Code\User\chatLanguageModels.json` |
| --- | --- |
| macOS | `~/Library/Application Support/Code/User/chatLanguageModels.json` |
| Linux | `~/.config/Code/User/chatLanguageModels.json` |

It is user-scope, so in a remote window it stays on the *client* while the
requests leave from the remote host. Only the API key lives elsewhere, in
secret storage, which is what `${input:...}` refers to. Fill one entry per
model from `scripts/discover_models.py`:

```json
[
  {
    "name": "codex-gateway",
    "vendor": "customendpoint",
    "apiKey": "${input:chat.lm.secret.<generated>}",
    "apiType": "chat-completions",
    "models": [
      {
        "id": "gpt-5.6-sol",
        "name": "GPT-5.6 Sol (Codex Gateway)",
        "url": "http://<gateway-host>/v1",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 128000,
        "maxOutputTokens": 16000,
        "supportsReasoningEffort": ["low", "medium", "high", "xhigh", "max"]
      },
      {
        "id": "gpt-5.4-mini",
        "name": "GPT-5.4 Mini (Codex Gateway)",
        "url": "http://<gateway-host>/v1",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 128000,
        "maxOutputTokens": 16000,
        "supportsReasoningEffort": ["low", "medium", "high", "xhigh"]
      }
    ]
  }
]
```

Four things that are easy to get wrong:

- **`url` is a base, not an endpoint.** Chat appends the path itself, and
  probes `<url>/models` to discover the catalogue. A URL ending in
  `/responses` asks for `/v1/responses/models` and 404s before any prompt is
  sent. Keep the `/v1`. Behind a reverse proxy the base carries no port —
  `http://<gateway-host>/v1` rather than `http://<gateway-host>:8085/v1`; see
  [Reaching it from another machine](#reaching-it-from-another-machine).
- **`apiType` is set on the group, not just the model.** The dialog writes it
  above `models`. A model-level key overrides the group, and the URL path is
  consulted last — so editing the model and leaving the group alone changes
  nothing.
- **Either API type works.** `chat-completions` is translated to `/responses`;
  `responses` is proxied. Both are normalized identically, so use whichever the
  dialog gave you.
- **`maxInputTokens` and `maxOutputTokens` are the client's own bookkeeping.**
  The backend rejects `max_output_tokens` on the wire and the gateway strips
  it, so these only shape how Chat trims context before sending.

The schema requires `id`, `name`, `url`, `toolCalling`, `vision`,
`maxOutputTokens`, and one of `maxInputTokens` or `contextWindow`; omit any and
the editor marks the entry invalid.

`supportsReasoningEffort` adds a Thinking Effort control next to the model.
`reasoning` is the one tuning parameter the backend accepts, so that choice
reaches it; `temperature` and `top_p` do not and are dropped. See
[Behaviour worth knowing](#behaviour-worth-knowing). The valid levels are
`low`, `medium`, `high`, `xhigh`, and `max` on the models that carry it —
`discover_models.py` prints them per model. `minimal` and `ultra` appear
elsewhere in Codex but `/responses` rejects both.

**The request comes from wherever the Chat extension runs, not from the window
you are looking at.** In a Remote-SSH, Tunnel, or Dev Container window that is
the remote host, so the URL has to resolve *there*. `chatLanguageModels.json`
meanwhile stays on the client, in the local VS Code user folder — the two live
on opposite sides of the connection.

## ACP in VS Code

ACP is an agent-to-editor protocol, not another HTTP endpoint. It gives Codex
its *own* agent panel — Codex App Server owns the loop, its tools, its sandbox,
and its approval modes. That is the reason to install it, and the reason not
to: it adds no model to the Chat picker, and for using these models in ordinary
editor work the section above is the shorter path. This repository
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

On Windows, use `.venv\Scripts\codex-gateway-acp.exe` and
`.venv\Scripts\codex-gateway.exe acp`.

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
its pinned `npx` package. On Windows, `npm install -g` installs a
`codex-acp.cmd` file, not a program file. Use that file, or the `dist\index.js`
file in the package. The launcher starts a `.js` file with `node`.

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
| `CODEX_ACP_BIN` | *(unset)* | Explicit adapter executable, or a `.js` entry point to run through `node`. If unset, the launcher uses pinned `npx`. |
| `CODEX_ACP_PACKAGE` | `@agentclientprotocol/codex-acp@1.1.9` | Package used by `npx`, as an exact `name@version`. Override only to test a deliberate adapter version. |
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
  See [Installing into a remote extension host](#installing-into-a-remote-extension-host)
  — `code --install-extension` on that host does **not** do this.
- If the gateway runs on another machine, keep its port on loopback and use an
  SSH tunnel such as `ssh -N -L 8085:localhost:8085 user@remote-host`; the ACP
  URL remains `http://127.0.0.1:8085/v1`.
- `CODEX_ADMIN_TOKEN` does not protect model use. Keep the provider port on a
  trusted interface exactly as described in the security section below.
- Prompts, selected workspace context, tool results, and permitted command/file
  operations pass through Codex App Server to the remote ChatGPT/Codex backend.
  The local extension does not make the model interaction offline or private.
- A repository on the Windows file system has no boundary. Use the procedure in
  the next section. WSL and Dev Containers are boundaries: install the launcher
  on the side that holds the code.

### Installing into a remote extension host

Remote SSH, Tunnels, and Dev Containers split the editor in two. The UI runs on
your machine; anything with a `main` — this extension, and Chat itself — runs in
the remote extension host. Both halves are called "VS Code" and they read
different directories, so the install commands above can silently address the
wrong one.

| On the host holding the code | Desktop VS Code | Remote extension host |
| --- | --- | --- |
| Extensions | `~/.vscode/extensions` | `~/.vscode-server/extensions` |
| Machine settings | user `settings.json` | `~/.vscode-server/data/Machine/settings.json` |

`code --install-extension` writes to the first column. Worse, if the remote host
*also* has desktop VS Code installed, `/usr/bin/code` wins the PATH lookup even
inside a remote window's own terminal — the command reports success and the
icon never appears.

Install into the second column instead. From the client, use the Extensions
view's **Install from VSIX…** with the window connected, and VS Code places a
workspace extension correctly. From a shell on the remote host, use the
server's own CLI rather than `code`:

```bash
ls ~/.vscode-server                     # exists => the window is remote
SERVER=$(ls -dt ~/.vscode-server/cli/servers/Stable-*/server/bin \
                ~/.vscode-server/bin/*/bin 2>/dev/null | head -1)
"$SERVER/code-server" --install-extension ./codex-gateway-acp-vscode.vsix \
  --extensions-dir ~/.vscode-server/extensions --force
"$SERVER/code-server" --list-extensions --extensions-dir ~/.vscode-server/extensions
```

Settings follow the same split. In a remote window the user `settings.json`
comes from the *client*, so values that are only true on the remote host — the
absolute `launcherPath`, a gateway URL resolved from that side — belong in
`~/.vscode-server/data/Machine/settings.json`. Every `codexGateway.*` setting is
`resource`-scoped, so machine scope applies. That file often does not exist yet.

Reload the window afterwards.

### Windows

Use this procedure when the repository is on the Windows file system and you
open it in VS Code for Windows. You do not need WSL or a Dev Container. No part
of the chain is POSIX-only. The extension finds `Scripts\*.exe`. The adapter is
a Node script. `@openai/codex` supplies `win32-x64` and `win32-arm64` programs.

Install these first: Docker Desktop, Python 3.11 or later with `uv`, Node.js 20
or later, and VS Code 1.100 or later.

**1. Start the gateway.** Run these commands in PowerShell in the repository
root:

```powershell
docker compose up -d codex-gateway
curl.exe -f http://127.0.0.1:8085/health
```

Type `curl.exe`, not `curl`. The name `curl` is an alias for
`Invoke-WebRequest`, and `Invoke-WebRequest` does not accept these options. If
the health check gives `503`, open <http://localhost:8085/> and sign in. Then
do the health check again.

**2. Build the launcher.** On Windows, `uv` puts the launcher in `Scripts`:

```powershell
uv sync --frozen
.\.venv\Scripts\codex-gateway-acp.exe --help
```

Use `--help` for this test. If you start the launcher with no options, it waits
for an ACP client and prints nothing.

**3. Build and install the extension:**

```powershell
cd vscode-extension
npm test
npm run package
code --install-extension .\codex-gateway-acp-vscode.vsix --force
```

Then reload VS Code.

**4. Set the launcher path** in the user `settings.json` file. In JSON, write
each backslash two times, or use forward slashes:

```json
{
  "codexGateway.launcherPath": "C:\\src\\codex-gateway\\.venv\\Scripts\\codex-gateway-acp.exe",
  "codexGateway.gatewayUrl": "http://127.0.0.1:8085/v1",
  "codexGateway.model": "gpt-5.6-sol",
  "codexGateway.initialMode": "read-only",
  "codexGateway.permissionPolicy": "ask"
}
```

If you open this repository as the workspace, do not set `launcherPath`. The
extension finds `.venv\Scripts\codex-gateway-acp.exe` without help.

**5. Send a task.** Open a local folder and trust it. Click **Codex Gateway**
in the Activity Bar. Send a task. To read the diagnostic messages, run the
command **Codex Gateway: Show ACP Logs**.

To prevent a download at the first connection, install the adapter before you
start. On Windows, `npm install -g` installs a `codex-acp.cmd` file, not a
program file. Set `codexGateway.adapterPath` to that file, or to the
`dist\index.js` file in the package. The launcher starts a `.js` file with
`node`.

Three parts of the launcher operate differently on Windows. You do not
configure any of them:

- **The launcher does not replace its own process.** Windows has no `exec`. The
  `os.execvpe` function starts a new process and stops the current process. An
  ACP client reads this as an agent that stops one second after it connects. On
  Windows, the launcher starts the adapter as a child process and waits for it.
  The launcher then returns the exit code of the adapter. The adapter keeps the
  three standard handles, so the adapter writes to the ACP stdout directly.
- **The launcher puts the adapter in a job object with the
  `KILL_ON_JOB_CLOSE` limit.** VS Code stops a child process with
  `TerminateProcess`. A program cannot intercept `TerminateProcess`. Without
  the job object, each **Restart ACP Agent** command leaves a Codex process
  that holds the pipes open.
- **`CODEX_ACP_PACKAGE` must contain an exact `name@version` value.** On
  Windows, `npx` is the `npx.cmd` file. Windows starts a command file with
  `cmd.exe`, and `cmd.exe` reads the arguments a second time. The launcher
  refuses a version range such as `^1.1.9`.

Unit tests cover this Windows behaviour. Nobody has yet run the procedure on a
Windows computer.

#### When the repository is in WSL

Use WSL only when the repository is in the WSL file system and you open it with
the VS Code WSL remote. Do steps 1 to 5 in the distribution, and use
distribution paths. Three problems can occur:

- Keep the repository, `uv`, Node.js and the launcher in the distribution. Set
  `codexGateway.launcherPath` to the path in the distribution. If the
  repository is on the Windows file system, use the procedure above instead.
- `code --install-extension` needs `wget`. Without `wget`, the command builds
  the VSIX file and then gives the error *"Failed to download the VS Code
  server"*. To install it, run
  `sudo apt-get update && sudo apt-get install -y wget`. As an alternative,
  press Ctrl+Shift+P, select **Extensions: Install from VSIX…**, and install
  the file from the WSL window.
- **WSL does not use the Windows VPN resolver.** A private name can resolve in
  the browser but not in the distribution. The launcher then cannot connect to
  the gateway. Add the host to `/etc/hosts` in the distribution. As an
  alternative, set `[network] generateResolvConf = false` in `/etc/wsl.conf`
  and point `/etc/resolv.conf` at the DNS server of the VPN.

### ACP troubleshooting

- **Gateway health is 503:** finish the gateway's device-code login; the ACP
  adapter deliberately has no separate ChatGPT credential.
- **No Codex Gateway icon:** install the generated VSIX, reload VS Code, and
  open a trusted local workspace. Web-only and untrusted workspaces are
  intentionally unsupported. In a Remote SSH, Tunnel, or Dev Container window,
  check *which* VS Code received the VSIX — see
  [Installing into a remote extension host](#installing-into-a-remote-extension-host).
- **Models missing from the Chat picker:** that is the other integration.
  The ACP client contributes a panel, never a model. See
  [VS Code Chat](#vs-code-chat).
- **`codex-acp` or `npx` not found:** install Node.js 20+ and the pinned npm
  package, then set `codexGateway.adapterPath` to its absolute executable path.
- **The launcher cannot be found:** set `codexGateway.launcherPath` to the
  absolute `.venv/bin/codex-gateway-acp` path on the extension host, or
  `.venv\Scripts\codex-gateway-acp.exe` on Windows.
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
| `CODEX_MODELS` | `gpt-5.6-sol,gpt-5.4` | Static `/models` catalogue. Generate the real one with `scripts/discover_models.py --env`. |
| `CODEX_BASE_URL` | ChatGPT backend | Upstream override. |
| `CODEX_UI` | `1` | `0` stops serving the reference page at `/`. |
| `CODEX_CA_FILE` | *(unset)* | PEM certificate to serve at `/ca.crt`. Set it when a private CA fronts this service, so clients can fetch the root instead of hunting for it. Nothing is guessed. |
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
- **A `system` role is rewritten to `developer`, on both surfaces.** The
  backend answers `400 {"detail":"System messages are not allowed"}`, most
  reliably for the canonical structured item VS Code Chat sends on every
  request. `developer` carries the same meaning and is accepted, so the
  request is fixed rather than failed. The chat-completions translation has
  always done this; `/responses` now does it too.
- **`temperature`, `top_p`, and `max_output_tokens` are dropped.** The
  reasoning backend rejects them outright — `400 {"detail":"Unsupported
  parameter: temperature"}` — rather than ignoring them, and nothing the
  gateway advertises would let a client discover that. VS Code Chat sends
  `temperature` on every request. `reasoning` is the one tuning parameter that
  survives, which is what makes a Thinking Effort selection mean something.

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

**Reverse proxy on a private name** — leave the port on loopback and let a
proxy that already faces the private network carry it. In Caddy that is three
lines:

```caddyfile
# Gateway stays on 127.0.0.1:8085; this block is the only way onto the VPN.
http://<gateway-name> {
	reverse_proxy 127.0.0.1:8085
}
```

Point the name at the host however that network resolves: a rewrite on the
VPN's DNS server, or `/etc/hosts` on each peer. Clients then use
`http://<gateway-name>/v1`: no port in the URL, and no client to reconfigure
when the gateway moves hosts. Prefer it over binding an interface, for two
reasons:

- **`127.0.0.1:8085` keeps answering on the gateway host.** `CODEX_GATEWAY_BIND`
  moves the published port; it does not add one.
- **No firewall rule of its own.** Caddy already listens on a port the subnet
  can reach, so there is no second hole to scope and audit.

Completions stream as server-sent events, so the proxy has to flush
`text/event-stream` as it arrives instead of buffering the response. Caddy does
that by default. With nginx, set `proxy_buffering off`.

The warning above applies here too, and applies harder. A name resolves for
every peer on the subnet; a loopback port does not. `CODEX_ADMIN_TOKEN` guards
`/auth/*` only, so anyone who can resolve and reach the name can spend the
subscription. Plain HTTP is defensible when the only path to that name is a
WireGuard subnet already encrypting every byte. On a wider network, terminate
TLS and serve the root at `/ca.crt` with `CODEX_CA_FILE`, so clients fetch it
instead of hunting for it.

### Driving it from VS Code on another machine

The extension never speaks HTTP to the gateway. It spawns `codex-gateway-acp`
as a child process, and *that* process makes the request. So the launcher, Node,
and this checkout all have to exist on the machine holding your code — only
model traffic crosses the network. `extensionKind: workspace` pins the
extension to that same side.

1. **Reach the gateway.** Either bind a private interface as above, or leave it
   on loopback and tunnel, or front it with a reverse proxy on a private name.
   Confirm before going further; nothing below works until this does:

   ```bash
   curl -sf http://<gateway-host>:8085/health && echo OK
   curl -sf http://<gateway-name>/health && echo OK      # proxied: no port
   ```

2. **Check prerequisites:** Python 3.11+, Node.js 20+, VS Code 1.100+. On
   Windows, use the procedure in [Windows](#windows) instead of the commands
   below. Replace `127.0.0.1` with the gateway address from step 1.

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
   and nowhere else — `Scripts` in place of `bin` on Windows. Keep the `/v1` —
   Codex appends `/responses` to it.

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
