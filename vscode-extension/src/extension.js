"use strict";

const fs = require("node:fs");
const path = require("node:path");

const vscode = require("vscode");

const { AcpClient } = require("./acp-client");
const { spawnPeer } = require("./ndjson-rpc");
const { SessionState } = require("./session-state");

const VIEW_ID = "codexGateway.chat";
const MAX_PROMPT_LENGTH = 200_000;
const ACP_START_TIMEOUT_MS = 300_000;

function fileWorkspaceFolders() {
  return (vscode.workspace.workspaceFolders || []).filter(
    (folder) => folder.uri.scheme === "file",
  );
}

function expandWorkspaceFolder(value, workspaceFolder) {
  return value.replaceAll("${workspaceFolder}", workspaceFolder);
}

function resolvePathSetting(value, workspaceFolder) {
  const expanded = expandWorkspaceFolder(value.trim(), workspaceFolder);
  return path.isAbsolute(expanded) ? expanded : path.resolve(workspaceFolder, expanded);
}

function executableCandidates(workspaceFolder) {
  const executable = process.platform === "win32" ? "codex-gateway-acp.exe" : "codex-gateway-acp";
  const binDirectory = process.platform === "win32" ? "Scripts" : "bin";
  return [
    path.join(workspaceFolder, ".venv", binDirectory, executable),
    path.join(
      workspaceFolder,
      "services",
      "models",
      "codex-gateway",
      ".venv",
      binDirectory,
      executable,
    ),
  ];
}

function canExecute(candidate) {
  try {
    fs.accessSync(candidate, process.platform === "win32" ? fs.constants.F_OK : fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function resolveLauncher(config, workspaceFolder) {
  const configured = config.get("launcherPath", "").trim();
  if (configured) {
    const expanded = expandWorkspaceFolder(configured, workspaceFolder);
    if (
      path.isAbsolute(expanded) ||
      expanded.includes("/") ||
      expanded.includes("\\")
    ) {
      return resolvePathSetting(expanded, workspaceFolder);
    }
    return expanded;
  }
  return (
    executableCandidates(workspaceFolder).find(canExecute) || "codex-gateway-acp"
  );
}

function sessionPaths(config) {
  const folders = fileWorkspaceFolders();
  if (folders.length === 0) {
    throw new Error("Open a local filesystem workspace before starting Codex Gateway ACP.");
  }
  const first = folders[0].uri.fsPath;
  const configured = config.get("workingDirectory", "").trim();
  const cwd = configured ? resolvePathSetting(configured, first) : first;
  if (!path.isAbsolute(cwd)) {
    throw new Error("The ACP working directory must be an absolute filesystem path.");
  }
  const additionalDirectories = folders
    .map((folder) => folder.uri.fsPath)
    .filter((directory) => path.resolve(directory) !== path.resolve(cwd));
  return { cwd, additionalDirectories };
}

function permissionDetail(request) {
  const toolCall = request && request.toolCall;
  if (!toolCall || typeof toolCall.rawInput !== "object" || !toolCall.rawInput) {
    return undefined;
  }
  try {
    const text = JSON.stringify(toolCall.rawInput);
    return text.length > 500 ? `${text.slice(0, 500)}…` : text;
  } catch {
    return undefined;
  }
}

function validatedString(value, name, maxLength = 500) {
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    throw new Error(`Invalid ${name}.`);
  }
  return value;
}

class CodexGatewayViewProvider {
  constructor(
    context,
    output,
    { createPeer = spawnPeer, Client = AcpClient } = {},
  ) {
    this.context = context;
    this.output = output;
    this.createPeer = createPeer;
    this.Client = Client;
    this.state = new SessionState();
    this.view = null;
    this.peer = null;
    this.client = null;
    this.clientGeneration = null;
    this.startPromise = null;
    this.startGeneration = null;
    this.renderTimer = null;
    this.permissionTokens = new Set();
    this.permissionTail = Promise.resolve();
    this.pendingPromptRequests = new Set();
    this.generation = 0;
    this.disposed = false;
  }

  resolveWebviewView(view) {
    this.view = view;
    const mediaRoot = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [mediaRoot],
    };
    view.webview.html = this.#html(view.webview, mediaRoot);
    const listener = view.webview.onDidReceiveMessage((message) => {
      void this.#handleWebviewMessage(message);
    });
    view.onDidDispose(() => {
      listener.dispose();
      if (this.view === view) {
        this.view = null;
      }
    });
    this.#renderNow();
  }

  async newSession() {
    if (this.state.busy) {
      return;
    }
    const generation = this.generation;
    const previousSessionId = this.state.sessionId;
    const previousClient = this.client;
    try {
      this.state.beginOperation("Creating ACP session");
      this.#scheduleRender();
      const client = await this.#ensureClient(generation);
      if (!this.#isCurrent(generation) || this.client !== client) {
        return;
      }
      const config = this.#configuration();
      const { cwd, additionalDirectories } = sessionPaths(config);
      const session = await client.newSession({
        cwd,
        additionalDirectories,
        mcpServers: [],
      });
      if (!this.#isCurrent(generation) || this.client !== client) {
        return;
      }
      this.state.startSession(session);
      this.#log(`session created: ${session.sessionId}`);
      this.#scheduleRender();
      if (
        previousSessionId &&
        previousSessionId !== session.sessionId &&
        previousClient === client
      ) {
        void client.closeSession(previousSessionId).then(
          (closed) => {
            if (closed) {
              this.#log(`session closed: ${previousSessionId}`);
            }
          },
          (error) => {
            if (this.#isCurrent(generation) && this.client === client) {
              this.#log(
                `could not close replaced session ${previousSessionId}: ${
                  error instanceof Error ? error.message : String(error)
                }`,
              );
            }
          },
        );
      }
    } catch (error) {
      if (this.#isCurrent(generation)) {
        this.#fail(error);
      }
    }
    if (this.#isCurrent(generation)) {
      this.#scheduleRender();
    }
  }

  async prompt(text, requestId) {
    const promptRequestId =
      typeof requestId === "string" && requestId.length <= 100 ? requestId : null;
    if (promptRequestId) {
      this.pendingPromptRequests.add(promptRequestId);
    }
    if (this.state.busy) {
      this.#rejectPrompt(promptRequestId);
      return;
    }
    const generation = this.generation;
    let accepted = false;
    try {
      validatedString(text, "prompt", MAX_PROMPT_LENGTH);
      if (
        !this.state.sessionId ||
        !this.client ||
        this.clientGeneration !== generation
      ) {
        await this.newSession();
      }
      if (
        !this.#isCurrent(generation) ||
        !this.state.sessionId ||
        !this.client ||
        this.clientGeneration !== generation
      ) {
        return;
      }
      const client = this.client;
      const sessionId = this.state.sessionId;
      this.state.beginPrompt(text);
      accepted = true;
      this.#acceptPrompt(promptRequestId);
      this.#scheduleRender();
      const result = await client.prompt(sessionId, [
        { type: "text", text },
      ]);
      if (
        this.#isCurrent(generation) &&
        this.client === client &&
        this.state.sessionId === sessionId
      ) {
        this.state.completePrompt(result && result.stopReason);
      }
    } catch (error) {
      if (this.#isCurrent(generation)) {
        this.#fail(error);
      }
    } finally {
      if (this.#isCurrent(generation)) {
        this.#scheduleRender();
      }
      if (!accepted) {
        this.#rejectPrompt(promptRequestId);
      }
    }
  }

  async cancelTurn() {
    const generation = this.generation;
    if (
      !this.client ||
      this.clientGeneration !== generation ||
      !this.state.sessionId ||
      !this.state.markCancelling()
    ) {
      return;
    }
    const client = this.client;
    const sessionId = this.state.sessionId;
    for (const token of this.permissionTokens) {
      token.cancel();
    }
    this.#scheduleRender();
    try {
      await client.cancel(sessionId);
    } catch (error) {
      if (this.#isCurrent(generation) && this.client === client) {
        this.#fail(error);
      }
    }
  }

  async setConfigOption(configId, value) {
    if (!this.client || !this.state.sessionId || this.state.busy) {
      return;
    }
    const generation = this.generation;
    const client = this.client;
    const sessionId = this.state.sessionId;
    try {
      validatedString(configId, "configuration option ID");
      validatedString(value, "configuration option value", 2_000);
      this.state.beginOperation("Updating session configuration");
      this.#scheduleRender();
      const result = await client.setConfigOption(
        sessionId,
        configId,
        value,
      );
      if (
        !this.#isCurrent(generation) ||
        this.client !== client ||
        this.state.sessionId !== sessionId
      ) {
        return;
      }
      if (result && Array.isArray(result.configOptions)) {
        this.state.setConfigOptions(result.configOptions);
      }
      this.state.completeOperation();
    } catch (error) {
      if (this.#isCurrent(generation) && this.client === client) {
        this.#fail(error);
      }
    }
    if (this.#isCurrent(generation)) {
      this.#scheduleRender();
    }
  }

  async restart() {
    if (this.disposed) {
      return;
    }
    this.#invalidateClient("ACP agent restarted");
    this.state.reset();
    this.#scheduleRender();
    await this.newSession();
  }

  showLogs() {
    this.output.show(true);
  }

  configurationChanged() {
    if (this.client || this.startPromise || this.state.sessionId) {
      void this.restart();
    }
  }

  dispose() {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.generation += 1;
    this.startPromise = null;
    this.startGeneration = null;
    if (this.renderTimer) {
      clearTimeout(this.renderTimer);
      this.renderTimer = null;
    }
    this.#stopClient("Extension disposed");
  }

  async #handleWebviewMessage(message) {
    if (!message || typeof message !== "object" || typeof message.type !== "string") {
      return;
    }
    switch (message.type) {
      case "ready":
        this.#renderNow();
        break;
      case "prompt":
        if (typeof message.text === "string") {
          await this.prompt(message.text, message.requestId);
        }
        break;
      case "cancel":
        await this.cancelTurn();
        break;
      case "setConfigOption":
        if (typeof message.configId === "string" && typeof message.value === "string") {
          await this.setConfigOption(message.configId, message.value);
        }
        break;
      default:
        this.#log(`ignored unknown webview message: ${message.type}`);
    }
  }

  async #ensureClient(generation) {
    if (!this.#isCurrent(generation)) {
      throw new Error("ACP operation was superseded.");
    }
    if (this.client && this.clientGeneration === generation) {
      return this.client;
    }
    if (this.startPromise && this.startGeneration === generation) {
      return this.startPromise;
    }
    const pending = this.#startClient(generation);
    this.startPromise = pending;
    this.startGeneration = generation;
    try {
      return await pending;
    } finally {
      if (this.startPromise === pending) {
        this.startPromise = null;
        this.startGeneration = null;
      }
    }
  }

  async #startClient(generation) {
    if (!this.#isCurrent(generation)) {
      throw new Error("ACP operation was superseded.");
    }
    if (!vscode.workspace.isTrusted) {
      throw new Error("Trust this workspace before launching the ACP coding agent.");
    }
    const folders = fileWorkspaceFolders();
    if (folders.length === 0) {
      throw new Error("Open a local filesystem workspace before launching the ACP agent.");
    }
    const config = this.#configuration();
    const first = folders[0].uri.fsPath;
    const launcher = resolveLauncher(config, first);
    const args = config.get("launcherArgs", []);
    if (!Array.isArray(args) || !args.every((argument) => typeof argument === "string")) {
      throw new Error("codexGateway.launcherArgs must contain only strings.");
    }
    const env = {
      ...process.env,
      CODEX_GATEWAY_URL: config.get("gatewayUrl", "http://127.0.0.1:8085/v1"),
      CODEX_GATEWAY_MODEL: config.get("model", "gpt-5.6-sol"),
      INITIAL_AGENT_MODE: config.get("initialMode", "read-only"),
    };
    const adapterPath = config.get("adapterPath", "").trim();
    if (adapterPath) {
      env.CODEX_ACP_BIN = resolvePathSetting(adapterPath, first);
    } else {
      delete env.CODEX_ACP_BIN;
    }
    const { cwd } = sessionPaths(config);
    const logProtocol = config.get("logProtocol", false);
    this.state.beginOperation("Starting ACP agent");
    this.#scheduleRender();
    this.#log(`starting ACP launcher: ${launcher}`);

    const peer = this.createPeer(launcher, args, {
      cwd,
      env,
      shell: false,
      logTraffic: logProtocol,
      onStderr: (text) => this.#appendStderr(text),
      onLog: logProtocol ? (text) => this.#log(text) : undefined,
    });
    this.peer = peer;
    const client = new this.Client(peer, {
      clientInfo: {
        name: "codex-gateway-vscode",
        title: "Codex Gateway for VS Code",
        version: this.context.extension.packageJSON.version,
      },
      onSessionUpdate: (sessionId, update) => {
        if (
          this.#isCurrent(generation) &&
          this.client === client &&
          sessionId === this.state.sessionId
        ) {
          this.state.applyUpdate(update);
          this.#scheduleRender();
        }
      },
      onPermissionRequest: (request) => this.#queuePermission(request, generation),
      onNotification: (method) => this.#log(`ignored ACP notification: ${method}`),
      onError: (error) => this.#log(error instanceof Error ? error.message : String(error)),
    });
    this.client = client;
    this.clientGeneration = generation;
    if (typeof peer.on === "function") {
      peer.on("close", (error) => {
        if (
          this.#isCurrent(generation) &&
          this.client === client
        ) {
          this.#invalidateClient("ACP agent process exited");
          this.#fail(error || new Error("The ACP agent process exited."));
          this.#scheduleRender();
        }
      });
    }
    try {
      const initialized = await client.initialize({ timeoutMs: ACP_START_TIMEOUT_MS });
      if (!this.#isCurrent(generation) || this.client !== client) {
        client.dispose("ACP startup was superseded");
        throw new Error("ACP operation was superseded.");
      }
      const version = initialized && initialized.agentInfo && initialized.agentInfo.version;
      this.#log(`ACP initialized${version ? ` (${version})` : ""}`);
      this.state.setStatus("ready", "Connected — creating session");
      return client;
    } catch (error) {
      if (this.client === client) {
        this.#stopClient("ACP initialization failed");
      } else {
        client.dispose("ACP initialization failed");
      }
      throw error;
    }
  }

  #queuePermission(request, generation) {
    const operation = this.permissionTail.then(() =>
      this.#requestPermission(request, generation),
    );
    this.permissionTail = operation.catch(() => {});
    return operation;
  }

  async #requestPermission(request, generation) {
    if (!this.#isCurrent(generation)) {
      return { outcome: "cancelled" };
    }
    const config = this.#configuration();
    const options = Array.isArray(request.options) ? request.options : [];
    if (config.get("permissionPolicy", "ask") === "deny") {
      const rejection = options.find((option) => option.kind === "reject_once");
      return rejection
        ? { outcome: "selected", optionId: rejection.optionId }
        : { outcome: "cancelled" };
    }

    const tokenSource = new vscode.CancellationTokenSource();
    this.permissionTokens.add(tokenSource);
    const title =
      request.toolCall && typeof request.toolCall.title === "string"
        ? request.toolCall.title
        : "Codex requests permission";
    const detail = permissionDetail(request);
    try {
      const picked = await vscode.window.showQuickPick(
        options.map((option) => ({
          label: option.name || option.optionId,
          description: option.kind ? option.kind.replaceAll("_", " ") : undefined,
          detail,
          optionId: option.optionId,
        })),
        {
          title,
          placeHolder: "Choose whether Codex may perform this operation",
          ignoreFocusOut: true,
        },
        tokenSource.token,
      );
      if (!this.#isCurrent(generation)) {
        return { outcome: "cancelled" };
      }
      return picked && typeof picked.optionId === "string"
        ? { outcome: "selected", optionId: picked.optionId }
        : { outcome: "cancelled" };
    } finally {
      this.permissionTokens.delete(tokenSource);
      tokenSource.dispose();
    }
  }

  #configuration() {
    const folder = fileWorkspaceFolders()[0];
    return vscode.workspace.getConfiguration("codexGateway", folder && folder.uri);
  }

  #isCurrent(generation) {
    return !this.disposed && generation === this.generation;
  }

  #invalidateClient(reason) {
    for (const requestId of this.pendingPromptRequests) {
      this.#postPromptResult("promptRejected", requestId);
    }
    this.pendingPromptRequests.clear();
    this.generation += 1;
    this.startPromise = null;
    this.startGeneration = null;
    this.#stopClient(reason);
  }

  #stopClient(reason) {
    for (const token of this.permissionTokens) {
      token.cancel();
      token.dispose();
    }
    this.permissionTokens.clear();
    const client = this.client;
    const peer = this.peer;
    this.client = null;
    this.clientGeneration = null;
    this.peer = null;
    if (client) {
      client.dispose(reason);
    } else if (peer) {
      peer.dispose(reason);
    }
  }

  #acceptPrompt(requestId) {
    if (requestId && this.pendingPromptRequests.delete(requestId)) {
      this.#postPromptResult("promptAccepted", requestId);
    }
  }

  #rejectPrompt(requestId) {
    if (requestId && this.pendingPromptRequests.delete(requestId)) {
      this.#postPromptResult("promptRejected", requestId);
    }
  }

  #postPromptResult(type, requestId) {
    if (this.view) {
      void this.view.webview.postMessage({ type, requestId });
    }
  }

  #fail(error) {
    const normalized = error instanceof Error ? error : new Error(String(error));
    this.state.setError(normalized);
    this.#log(normalized.stack || normalized.message);
  }

  #appendStderr(text) {
    const value = String(text);
    this.output.append(value);
  }

  #log(message) {
    this.output.appendLine(`[${new Date().toISOString()}] ${message}`);
  }

  #scheduleRender() {
    if (this.renderTimer || !this.view) {
      return;
    }
    this.renderTimer = setTimeout(() => {
      this.renderTimer = null;
      this.#renderNow();
    }, 50);
  }

  #renderNow() {
    if (!this.view) {
      return;
    }
    const canPrompt =
      vscode.workspace.isTrusted && fileWorkspaceFolders().length > 0 && !this.disposed;
    void this.view.webview.postMessage({
      type: "state",
      state: this.state.snapshot({ canPrompt }),
    });
  }

  #html(webview, mediaRoot) {
    const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, "view.css"));
    const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, "view.js"));
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src ${webview.cspSource};">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${styleUri}">
  <title>Codex Gateway ACP</title>
</head>
<body>
  <div id="status" class="status" data-state="idle" role="status" aria-live="polite">
    <span class="status-dot" aria-hidden="true"></span>
    <span id="status-text">Not connected</span>
  </div>
  <div id="error" class="error" role="alert"></div>
  <section id="config" class="config" aria-label="Session configuration"></section>
  <section id="plan" class="plan" hidden>
    <div class="plan-title">Plan</div>
    <ol id="plan-items"></ol>
  </section>
  <main id="transcript" class="transcript" aria-live="polite"></main>
  <div id="empty" class="empty">Send a task to start an ACP session through codex-gateway.</div>
  <section class="composer" aria-label="Prompt composer">
    <textarea id="prompt" aria-label="Message Codex" placeholder="Describe the coding task…"></textarea>
    <div class="actions">
      <button id="cancel" class="secondary" type="button" disabled>Cancel</button>
      <button id="send" type="button" disabled>Send</button>
    </div>
  </section>
  <script src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function activate(context) {
  const output = vscode.window.createOutputChannel("Codex Gateway ACP");
  const provider = new CodexGatewayViewProvider(context, output);
  context.subscriptions.push(
    output,
    provider,
    vscode.window.registerWebviewViewProvider(VIEW_ID, provider),
    vscode.commands.registerCommand("codexGateway.newSession", () => provider.newSession()),
    vscode.commands.registerCommand("codexGateway.cancelTurn", () => provider.cancelTurn()),
    vscode.commands.registerCommand("codexGateway.restartAgent", () => provider.restart()),
    vscode.commands.registerCommand("codexGateway.showLogs", () => provider.showLogs()),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("codexGateway")) {
        provider.configurationChanged();
      }
    }),
  );
}

module.exports = {
  activate,
  CodexGatewayViewProvider,
  executableCandidates,
  resolveLauncher,
  sessionPaths,
};
