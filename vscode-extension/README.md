# Codex Gateway ACP for VS Code

This directory contains the repository-owned VS Code client for
`codex-gateway-acp`. It implements ACP v1 directly over newline-delimited
JSON-RPC and uses only the official VS Code Extension API and Node.js built-ins
at runtime. It does not install or load a Marketplace ACP client.

The extension runs in the workspace extension host, launches
`codex-gateway-acp` without a command shell, and renders the conversation in a
dedicated Activity Bar view. ACP permission requests are shown with VS Code's
trusted Quick Pick UI, never inside the webview.

## Test and package

VS Code 1.100 or newer is required to install the extension. Node.js 20 or
newer is required for the pinned official VS Code packaging tool.

```bash
npm test
npm run package
code --install-extension ./codex-gateway-acp-vscode.vsix --force
```

`npm run package` invokes Microsoft's pinned `@vscode/vsce` package. The
resulting VSIX is ignored by Git and does not update automatically; rebuild and
reinstall it after changing this directory.

## Use

1. Open a trusted local workspace in VS Code.
2. Open the **Codex Gateway** icon in the Activity Bar.
3. Send a task. The extension lazily starts the ACP launcher and creates a
   session rooted at the first workspace folder.
4. Use the selectors above the transcript to change mode, model, reasoning,
   and other configuration published by the agent.
5. Use the view toolbar to start a fresh session, cancel the current turn, or
   restart and immediately reconnect the agent process.

The launcher is auto-discovered in either `.venv/bin` under the workspace or
`services/models/codex-gateway/.venv/bin` in the monorepo. On Windows the
extension looks in `Scripts` for `codex-gateway-acp.exe`. Otherwise it falls
back to `codex-gateway-acp` on the extension host's `PATH`. Configure an
explicit path with `codexGateway.launcherPath`.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `codexGateway.launcherPath` | auto-discover | ACP launcher executable; supports `${workspaceFolder}`. |
| `codexGateway.launcherArgs` | `[]` | Direct arguments; no shell is used. |
| `codexGateway.gatewayUrl` | `http://127.0.0.1:8085/v1` | Gateway URL visible from the extension host. |
| `codexGateway.model` | `gpt-5.6-sol` | Initial model for new sessions. |
| `codexGateway.initialMode` | `read-only` | Initial approval and sandbox preset. |
| `codexGateway.adapterPath` | unset | Optional explicit `codex-acp` executable. |
| `codexGateway.workingDirectory` | first workspace | Session working directory. |
| `codexGateway.permissionPolicy` | `ask` | Ask in VS Code UI or deny every permission request. |
| `codexGateway.logProtocol` | `false` | Log full ACP frames for diagnostics. |

All file-scheme workspace roots except the selected working directory are
passed as ACP additional directories when the agent advertises that capability.

## Security boundaries

- The extension declares untrusted and virtual workspaces unsupported.
- The ACP child process and all conversation state live in the extension host;
  the webview is only a renderer.
- Prompts, selected source context, tool results, and permitted file/command
  operations pass through Codex App Server and `codex-gateway` to the remote
  ChatGPT/Codex backend. This integration is not an offline or private model.
- The webview loads local extension assets under a restrictive content security
  policy and renders untrusted text with `textContent`.
- `read-only` is the default. **Agent** permits ordinary workspace edits and
  sandboxed commands without asking every time. **Agent Full Access** disables
  those safeguards and should be used only for a deliberately trusted task.
- Protocol logging can contain prompts, source, diffs, command output, and
  permission details. Leave it disabled unless actively troubleshooting.
