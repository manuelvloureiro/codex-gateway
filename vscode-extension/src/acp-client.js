"use strict";

const ACP_PROTOCOL_VERSION = 1;
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000;

function assertPeer(peer) {
  if (!peer || typeof peer.request !== "function" || typeof peer.notify !== "function") {
    throw new TypeError("AcpClient requires an RPC peer with request() and notify()");
  }
}

function assertNonEmptyString(value, name) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${name} must be a non-empty string`);
  }
}

function promptBlocks(prompt) {
  if (typeof prompt === "string") {
    return [{ type: "text", text: prompt }];
  }
  if (Array.isArray(prompt)) {
    return prompt;
  }
  if (prompt && typeof prompt === "object") {
    return [prompt];
  }
  throw new TypeError("prompt must be a string, content block, or array of content blocks");
}

function permissionResult(value, options) {
  if (value && value.outcome && typeof value.outcome === "object") {
    return value;
  }

  if (value && (value.outcome === "selected" || value.outcome === "cancelled")) {
    return { outcome: value };
  }

  let optionId = null;
  if (typeof value === "string") {
    optionId = value;
  } else if (value && typeof value.optionId === "string") {
    optionId = value.optionId;
  }

  if (optionId !== null) {
    const offered = Array.isArray(options)
      ? options.some((option) => option && option.optionId === optionId)
      : false;
    if (!offered) {
      throw new Error(`Permission handler selected an unknown option: ${optionId}`);
    }
    return { outcome: { outcome: "selected", optionId } };
  }

  return { outcome: { outcome: "cancelled" } };
}

class AcpClient {
  constructor(
    peer,
    {
      clientInfo = { name: "codex-gateway-vscode", version: "0.1.0" },
      onSessionUpdate = () => {},
      onPermissionRequest = null,
      onNotification = () => {},
      onError = () => {},
      requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    } = {},
  ) {
    assertPeer(peer);
    this.peer = peer;
    this.clientInfo = clientInfo;
    this.onSessionUpdate = onSessionUpdate;
    this.onPermissionRequest = onPermissionRequest;
    this.onNotification = onNotification;
    this.onError = onError;
    this.requestTimeoutMs = requestTimeoutMs;

    this.initializeResult = null;
    this.initializePromise = null;
    this.promptTails = new Map();
    this.pendingPermissions = new Map();
    this.pendingNewSessions = 0;
    this.activatingSessions = new Set();
    this.knownSessions = new Set();
    this.bufferedSessionUpdates = new Map();
    this.disposed = false;

    this.handleNotification = this.handleNotification.bind(this);
    this.handleRequest = this.handleRequest.bind(this);
    this.#attachPeerHandlers();
  }

  async initialize({ clientInfo = this.clientInfo, signal, timeoutMs } = {}) {
    this.#assertActive();
    if (this.initializeResult) {
      return this.initializeResult;
    }
    if (this.initializePromise) {
      return this.initializePromise;
    }

    this.initializePromise = this.peer
      .request(
        "initialize",
        {
          protocolVersion: ACP_PROTOCOL_VERSION,
          clientCapabilities: {
            fs: { readTextFile: false, writeTextFile: false },
            terminal: false,
          },
          clientInfo,
        },
        this.#requestOptions({ signal, timeoutMs }),
      )
      .then((result) => {
        if (!result || result.protocolVersion !== ACP_PROTOCOL_VERSION) {
          const received = result && result.protocolVersion;
          throw new Error(`Unsupported ACP protocol version: ${String(received)}`);
        }
        this.initializeResult = result;
        return result;
      })
      .finally(() => {
        if (!this.initializeResult) {
          this.initializePromise = null;
        }
      });

    return this.initializePromise;
  }

  async newSession(
    { cwd, mcpServers = [], additionalDirectories = [] },
    { signal, timeoutMs } = {},
  ) {
    assertNonEmptyString(cwd, "cwd");
    const initialized = await this.initialize({ signal, timeoutMs });
    const params = { cwd, mcpServers };
    if (
      this.#supportsAdditionalDirectories(initialized) &&
      Array.isArray(additionalDirectories) &&
      additionalDirectories.length > 0
    ) {
      params.additionalDirectories = additionalDirectories;
    }
    this.pendingNewSessions += 1;
    try {
      const result = await this.peer.request(
        "session/new",
        params,
        this.#requestOptions({ signal, timeoutMs }),
      );
      if (!result || typeof result.sessionId !== "string") {
        throw new Error("ACP session/new response did not include a sessionId");
      }
      this.activatingSessions.add(result.sessionId);
      this.#scheduleSessionActivation(result.sessionId);
      return result;
    } finally {
      this.pendingNewSessions -= 1;
      if (this.pendingNewSessions === 0) {
        this.#scheduleOrphanUpdateFlush();
      }
    }
  }

  async loadSession(
    { sessionId, cwd, mcpServers = [] },
    { signal, timeoutMs } = {},
  ) {
    assertNonEmptyString(sessionId, "sessionId");
    assertNonEmptyString(cwd, "cwd");
    const initialized = await this.initialize({ signal, timeoutMs });
    if (!initialized.agentCapabilities || initialized.agentCapabilities.loadSession !== true) {
      throw new Error("ACP agent does not support session/load");
    }
    this.knownSessions.add(sessionId);
    return this.peer.request(
      "session/load",
      { sessionId, cwd, mcpServers },
      this.#requestOptions({ signal, timeoutMs }),
    );
  }

  prompt(sessionId, prompt, { signal, timeoutMs } = {}) {
    this.#assertActive();
    assertNonEmptyString(sessionId, "sessionId");
    this.knownSessions.add(sessionId);
    const content = promptBlocks(prompt);
    const previous = this.promptTails.get(sessionId) || Promise.resolve();
    const operation = previous.then(() => {
      this.#assertActive();
      if (signal && signal.aborted) {
        throw signal.reason || new Error("Prompt aborted");
      }
      return this.peer.request(
        "session/prompt",
        { sessionId, prompt: content },
        this.#requestOptions({
          signal,
          // A coding turn may legitimately run for many minutes or wait for
          // the user to answer a permission request. Cancellation, rather
          // than a short transport timeout, terminates prompt turns.
          timeoutMs: timeoutMs === undefined ? 0 : timeoutMs,
        }),
      );
    });

    const tail = operation.then(
      () => this.#removePromptTail(sessionId, tail),
      () => this.#removePromptTail(sessionId, tail),
    );
    this.promptTails.set(sessionId, tail);
    return operation;
  }

  async cancel(sessionId) {
    this.#assertActive();
    assertNonEmptyString(sessionId, "sessionId");
    const operations = [this.peer.notify("session/cancel", { sessionId })];
    for (const pending of this.pendingPermissions.values()) {
      if (pending.sessionId === sessionId) {
        operations.push(pending.cancel());
      }
    }
    await Promise.all(operations);
  }

  async closeSession(sessionId, { signal, timeoutMs } = {}) {
    this.#assertActive();
    assertNonEmptyString(sessionId, "sessionId");
    const initialized = await this.initialize({ signal, timeoutMs });
    const sessionCapabilities =
      initialized.agentCapabilities && initialized.agentCapabilities.sessionCapabilities;
    if (!sessionCapabilities || !sessionCapabilities.close) {
      return false;
    }
    await this.peer.request(
      "session/close",
      { sessionId },
      this.#requestOptions({ signal, timeoutMs }),
    );
    this.knownSessions.delete(sessionId);
    this.activatingSessions.delete(sessionId);
    this.bufferedSessionUpdates.delete(sessionId);
    this.promptTails.delete(sessionId);
    return true;
  }

  setConfigOption(sessionId, configId, value, type, { signal, timeoutMs } = {}) {
    this.#assertActive();
    assertNonEmptyString(sessionId, "sessionId");
    assertNonEmptyString(configId, "configId");
    const params = { sessionId, configId, value };
    if (type !== undefined) {
      params.type = type;
    }
    return this.peer.request(
      "session/set_config_option",
      params,
      this.#requestOptions({ signal, timeoutMs }),
    );
  }

  setMode(sessionId, modeId, { signal, timeoutMs } = {}) {
    this.#assertActive();
    assertNonEmptyString(sessionId, "sessionId");
    assertNonEmptyString(modeId, "modeId");
    return this.peer.request(
      "session/set_mode",
      { sessionId, modeId },
      this.#requestOptions({ signal, timeoutMs }),
    );
  }

  handleNotification(message) {
    if (!message || typeof message.method !== "string") {
      return;
    }
    try {
      let handled = false;
      if (message.method === "session/update") {
        const params = message.params || {};
        if (this.#shouldBufferSessionUpdate(params.sessionId)) {
          const buffered = this.bufferedSessionUpdates.get(params.sessionId) || [];
          buffered.push(params);
          this.bufferedSessionUpdates.set(params.sessionId, buffered);
        } else {
          this.#deliverSessionUpdate(params);
        }
        handled = true;
      } else if (message.method === "$/cancel_request") {
        const requestId = message.params && message.params.requestId;
        const pending = this.pendingPermissions.get(requestId);
        if (pending) {
          void Promise.resolve(pending.cancel()).catch((error) => this.#reportError(error));
        }
        handled = true;
      }
      if (!handled) {
        this.onNotification(message.method, message.params, message);
      }
    } catch (error) {
      this.#reportError(error);
    }
  }

  async handleRequest(request) {
    if (!request || typeof request.method !== "string") {
      return;
    }
    if (request.method !== "session/request_permission") {
      await request.error(-32601, "Method not found", { method: request.method });
      return;
    }
    await this.#handlePermissionRequest(request);
  }

  dispose(reason = new Error("ACP client disposed")) {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    for (const pending of this.pendingPermissions.values()) {
      void Promise.resolve(pending.cancel()).catch((error) => this.#reportError(error));
    }
    this.pendingPermissions.clear();
    this.bufferedSessionUpdates.clear();
    if (typeof this.peer.setNotificationHandler === "function") {
      this.peer.setNotificationHandler(null);
    }
    if (typeof this.peer.setRequestHandler === "function") {
      this.peer.setRequestHandler(null);
    }
    if (typeof this.peer.dispose === "function") {
      this.peer.dispose(reason);
    }
  }

  async #handlePermissionRequest(request) {
    const params = request.params || {};
    let settled = false;
    let responsePromise = null;
    const finish = (result) => {
      if (settled) {
        return responsePromise || Promise.resolve();
      }
      settled = true;
      this.pendingPermissions.delete(request.id);
      try {
        responsePromise = Promise.resolve(request.respond(result));
      } catch (error) {
        responsePromise = Promise.reject(error);
      }
      return responsePromise;
    };
    const fail = (code, message, data) => {
      if (settled) {
        return responsePromise || Promise.resolve();
      }
      settled = true;
      this.pendingPermissions.delete(request.id);
      try {
        responsePromise = Promise.resolve(request.error(code, message, data));
      } catch (error) {
        responsePromise = Promise.reject(error);
      }
      return responsePromise;
    };
    this.pendingPermissions.set(request.id, {
      sessionId: params.sessionId,
      cancel: () => finish({ outcome: { outcome: "cancelled" } }),
    });

    let decision;
    try {
      decision = this.onPermissionRequest
        ? await this.onPermissionRequest(params)
        : null;
    } catch (error) {
      if (!settled) {
        await fail(-32603, "Permission handler failed", {
          message: error instanceof Error ? error.message : String(error),
        });
        return;
      }
      await responsePromise;
      return;
    }

    if (!settled) {
      await finish(permissionResult(decision, params.options));
    } else {
      await responsePromise;
    }
  }

  #supportsAdditionalDirectories(initializeResult) {
    const capabilities = initializeResult && initializeResult.agentCapabilities;
    return Boolean(
      capabilities &&
        capabilities.sessionCapabilities &&
        capabilities.sessionCapabilities.additionalDirectories,
    );
  }

  #attachPeerHandlers() {
    if (typeof this.peer.setNotificationHandler === "function") {
      this.peer.setNotificationHandler(this.handleNotification);
    }
    if (typeof this.peer.setRequestHandler === "function") {
      this.peer.setRequestHandler(this.handleRequest);
    }
  }

  #shouldBufferSessionUpdate(sessionId) {
    if (typeof sessionId !== "string" || this.knownSessions.has(sessionId)) {
      return false;
    }
    return this.activatingSessions.has(sessionId) || this.pendingNewSessions > 0;
  }

  #deliverSessionUpdate(params) {
    this.onSessionUpdate(params.sessionId, params.update, params);
  }

  #scheduleSessionActivation(sessionId) {
    setImmediate(() => {
      if (this.disposed) {
        return;
      }
      this.activatingSessions.delete(sessionId);
      this.knownSessions.add(sessionId);
      const buffered = this.bufferedSessionUpdates.get(sessionId) || [];
      this.bufferedSessionUpdates.delete(sessionId);
      for (const params of buffered) {
        try {
          this.#deliverSessionUpdate(params);
        } catch (error) {
          this.#reportError(error);
        }
      }
    });
  }

  #scheduleOrphanUpdateFlush() {
    setImmediate(() => {
      if (this.disposed || this.pendingNewSessions > 0) {
        return;
      }
      for (const [sessionId, buffered] of this.bufferedSessionUpdates) {
        if (this.activatingSessions.has(sessionId)) {
          continue;
        }
        this.bufferedSessionUpdates.delete(sessionId);
        for (const params of buffered) {
          try {
            this.#deliverSessionUpdate(params);
          } catch (error) {
            this.#reportError(error);
          }
        }
      }
    });
  }

  #requestOptions({ signal, timeoutMs }) {
    return {
      timeoutMs: timeoutMs === undefined ? this.requestTimeoutMs : timeoutMs,
      ...(signal ? { signal } : {}),
    };
  }

  #removePromptTail(sessionId, tail) {
    if (this.promptTails.get(sessionId) === tail) {
      this.promptTails.delete(sessionId);
    }
  }

  #reportError(error) {
    try {
      this.onError(error);
    } catch {
      // User callbacks must not break the protocol reader.
    }
  }

  #assertActive() {
    if (this.disposed) {
      throw new Error("ACP client is disposed");
    }
  }
}

module.exports = {
  ACP_PROTOCOL_VERSION,
  AcpClient,
  permissionResult,
  promptBlocks,
};
