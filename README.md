# codex-gateway

Serves a ChatGPT Plus/Pro subscription as a keyless, OpenAI-compatible
provider, so any client that speaks `chat/completions` can use it — including
VS Code's built-in Chat, which takes it as a custom endpoint.

The service is self-contained: one runtime dependency, `aiohttp`.

## Why it exists

ChatGPT's Codex backend is *not* an OpenAI-compatible API. Every rule below was
established by making the request and reading the rejection:

- `/responses` only — there is no `/chat/completions`
- `input` must be a list, `stream` must be true, `store` must be false
- `originator: codex_cli_rs` plus a matching User-Agent, or the backend serves
  a restricted surface
- `ChatGPT-Account-ID`, decoded from the access token's JWT payload
- upstream `/models` needs a `client_version` param and returns `[]` anyway,
  so the catalogue here is served from `CODEX_MODELS`

Model names are not the public ones: `gpt-5.6-sol` works, bare `gpt-5.6` comes
back "not supported when using Codex with a ChatGPT account". Run
`scripts/discover_models.py` for the list this account actually serves — the
app-server knows, even though the HTTP catalogue does not.

## Layout

```
src/codex_gateway/
  oauth.py      device-code login, token store, refresh   (no I/O beyond HTTP)
  translate.py  chat/completions <-> Responses            (pure functions)
  server.py     aiohttp app: provider routes + /auth/*
  login.py      CLI front end
app/index.html  reference UI (login/logout/test a message), served at /
scripts/        host-side tooling; model discovery via the Codex app-server
tests/          unit tests, no network, no real credential store
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

Chat's model picker accepts a custom provider, so these models sit beside the
built-in ones and drive ask, edit, and agent mode. Nothing gets installed to
reach that — the provider is configuration, not an extension.

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

## VS Code Chat troubleshooting

- **Gateway health is 503:** finish the device-code login. The gateway starts
  unauthenticated on purpose, and Chat has no separate ChatGPT credential.
- **No models in the picker:** the provider is added through Manage Models, not
  by installing anything. Check `chatLanguageModels.json` actually parses — a
  trailing comma silently drops the whole provider.
- **404 on a `/models` probe:** `url` must be the base, not an endpoint. Chat
  appends `/models` itself, so `/v1/responses` becomes `/v1/responses/models`.
- **400 "Unsupported parameter" or "System messages are not allowed":** the
  gateway normalizes both; a running instance predating that fix will not. Check
  the gateway log for the request it forwarded.
- **Connection refused, but curl works from your machine:** Chat runs
  workspace-side, so the request leaves from the remote host in a Remote SSH,
  Tunnel, or Dev Container window. The URL has to resolve *there*.

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

Nothing to install on the remote side: Chat's custom-endpoint provider speaks
HTTP straight to the gateway. Reach it by any of the three routes above, then
use that base URL in [VS Code Chat](#vs-code-chat).

Chat runs workspace-side, so in a Remote SSH, Tunnel, or Dev Container window
the request leaves from the *remote host* while `chatLanguageModels.json` stays
on the client. The URL must resolve on the remote host, not on the machine
showing you the window.

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
