"use strict";

const MAX_TEXT_LENGTH = 200_000;
const DEFAULT_MAX_CHARACTERS = 1_000_000;

function appendCapped(original, addition) {
  const combined = `${original || ""}${addition || ""}`;
  if (combined.length <= MAX_TEXT_LENGTH) {
    return combined;
  }
  return `[earlier output truncated]\n${combined.slice(-MAX_TEXT_LENGTH)}`;
}

function contentText(content) {
  if (!content || typeof content !== "object") {
    return "";
  }
  if (content.type === "text" && typeof content.text === "string") {
    return content.text;
  }
  if (content.type === "resource_link" && typeof content.uri === "string") {
    return `[Resource: ${content.name || content.uri}] ${content.uri}`;
  }
  if (content.type === "image") {
    return "[Image]";
  }
  if (content.type === "audio") {
    return "[Audio]";
  }
  if (content.type === "resource") {
    const resource = content.resource || {};
    if (typeof resource.text === "string") {
      return resource.text;
    }
    return `[Embedded resource: ${resource.uri || "unknown"}]`;
  }
  return "";
}

function toolContentText(contents) {
  const lines = [];
  for (const item of Array.isArray(contents) ? contents : []) {
    if (!item || typeof item !== "object") {
      continue;
    }
    if (item.type === "content") {
      const value = contentText(item.content);
      if (value) {
        lines.push(value);
      }
    } else if (item.type === "diff") {
      const path = item.path || "file";
      const oldText = typeof item.oldText === "string" ? item.oldText : "";
      const newText = typeof item.newText === "string" ? item.newText : "";
      lines.push(`Diff: ${path}\n--- before\n${oldText}\n+++ after\n${newText}`);
    }
  }
  return lines.join("\n");
}

function normalizeConfigOptions(options) {
  return (Array.isArray(options) ? options : []).filter(
    (option) => option && typeof option.id === "string",
  );
}

class SessionState {
  constructor({ maxItems = 240, maxCharacters = DEFAULT_MAX_CHARACTERS } = {}) {
    this.maxItems = maxItems;
    this.maxCharacters = maxCharacters;
    this.reset();
  }

  reset(session = {}) {
    this.sessionId = typeof session.sessionId === "string" ? session.sessionId : null;
    this.status = this.sessionId ? "ready" : "idle";
    this.statusText = this.sessionId ? "Ready" : "Not connected";
    this.busy = false;
    this.turnInProgress = false;
    this.cancelRequested = false;
    this.error = "";
    this.messages = [];
    this.tools = new Map();
    this.plan = [];
    this.sequence = 0;
    this.configOptions = normalizeConfigOptions(session.configOptions);
    this.title = "";
    this.usage = null;
  }

  startSession(session) {
    this.reset(session);
  }

  setStatus(status, statusText) {
    this.status = status;
    this.statusText = statusText;
    if (status !== "error") {
      this.error = "";
    }
  }

  beginOperation(statusText) {
    this.busy = true;
    this.turnInProgress = false;
    this.cancelRequested = false;
    this.setStatus("busy", statusText);
  }

  completeOperation(statusText = "Ready") {
    this.busy = false;
    this.turnInProgress = false;
    this.cancelRequested = false;
    this.setStatus("ready", statusText);
  }

  markCancelling() {
    if (!this.turnInProgress || this.cancelRequested) {
      return false;
    }
    this.cancelRequested = true;
    this.status = "busy";
    this.statusText = "Cancelling turn";
    return true;
  }

  beginPrompt(text) {
    this.busy = true;
    this.turnInProgress = true;
    this.cancelRequested = false;
    this.status = "busy";
    this.statusText = "Codex is working";
    this.error = "";
    this.messages.push({
      id: `user-${++this.sequence}`,
      role: "user",
      text,
      sequence: this.sequence,
      localEcho: true,
    });
    this.#trim();
  }

  completePrompt(stopReason) {
    this.busy = false;
    this.turnInProgress = false;
    this.cancelRequested = false;
    this.status = "ready";
    this.statusText = stopReason ? `Ready — ${stopReason.replaceAll("_", " ")}` : "Ready";
  }

  setError(error) {
    this.busy = false;
    this.turnInProgress = false;
    this.cancelRequested = false;
    this.status = "error";
    this.statusText = "ACP error";
    this.error = error instanceof Error ? error.message : String(error);
  }

  applyUpdate(update) {
    if (!update || typeof update !== "object") {
      return;
    }
    switch (update.sessionUpdate) {
      case "agent_message_chunk":
        this.#appendMessage("agent", update.messageId, contentText(update.content));
        break;
      case "agent_thought_chunk":
        this.#appendMessage("thought", update.messageId, contentText(update.content));
        break;
      case "user_message_chunk":
        this.#appendMessage("user", update.messageId, contentText(update.content));
        break;
      case "tool_call":
        this.#mergeTool(update);
        break;
      case "tool_call_update":
        this.#mergeTool(update);
        break;
      case "plan":
        this.plan = Array.isArray(update.entries) ? update.entries : [];
        break;
      case "plan_removed":
        this.plan = [];
        break;
      case "current_mode_update":
        this.#setConfigValue("mode", update.currentModeId);
        break;
      case "config_option_update":
        this.configOptions = normalizeConfigOptions(update.configOptions);
        break;
      case "session_info_update":
        if (Object.hasOwn(update, "title")) {
          this.title = typeof update.title === "string" ? update.title : "";
        }
        if (
          update._meta &&
          update._meta.codex &&
          update._meta.codex.error &&
          typeof update._meta.codex.error.message === "string"
        ) {
          const prefix = update._meta.codex.error.willRetry
            ? "Codex is retrying"
            : "Codex reported an error";
          this.statusText = `${prefix} — ${update._meta.codex.error.message}`;
        }
        break;
      case "usage_update":
        this.usage = update;
        break;
      default:
        break;
    }
    this.#trim();
  }

  setConfigOptions(options) {
    this.configOptions = normalizeConfigOptions(options);
  }

  #appendMessage(role, messageId, text) {
    if (!text) {
      return;
    }
    const id = typeof messageId === "string" ? messageId : null;
    const last = this.messages.at(-1);
    if (role === "user" && last && last.role === "user" && last.localEcho) {
      last.id = id || last.id;
      last.localEcho = false;
      last.suppressEcho = true;
      return;
    }
    if (role === "user" && last && last.suppressEcho && (!id || last.id === id)) {
      return;
    }
    if (last && ((id && last.id === id) || (!id && last.role === role))) {
      last.text = appendCapped(last.text, text);
      return;
    }
    this.messages.push({
      id: id || `${role}-${++this.sequence}`,
      role,
      text: appendCapped("", text),
      sequence: ++this.sequence,
    });
  }

  #mergeTool(update) {
    if (typeof update.toolCallId !== "string") {
      return;
    }
    const existing = this.tools.get(update.toolCallId) || {
      id: update.toolCallId,
      title: "Tool call",
      status: "pending",
      output: "",
      contentOutput: "",
      terminalOutput: "",
      sequence: ++this.sequence,
    };
    for (const field of ["title", "kind", "status"]) {
      if (typeof update[field] === "string") {
        existing[field] = update[field];
      }
    }
    if (Object.hasOwn(update, "content")) {
      existing.contentOutput = appendCapped("", toolContentText(update.content));
    }
    const metadata = update._meta || {};
    const delta = metadata.terminal_output_delta;
    const full = metadata.terminal_output;
    if (typeof delta === "string") {
      existing.terminalOutput = appendCapped(existing.terminalOutput, delta);
    } else if (delta && typeof delta.data === "string") {
      existing.terminalOutput = appendCapped(existing.terminalOutput, delta.data);
    } else if (typeof full === "string") {
      existing.terminalOutput = appendCapped(existing.terminalOutput, full);
    } else if (full && typeof full.data === "string") {
      existing.terminalOutput = appendCapped(existing.terminalOutput, full.data);
    }
    const mcpDelta = metadata.mcp_output_delta;
    if (typeof mcpDelta === "string") {
      existing.terminalOutput = appendCapped(existing.terminalOutput, mcpDelta);
    } else if (mcpDelta && typeof mcpDelta.data === "string") {
      existing.terminalOutput = appendCapped(existing.terminalOutput, mcpDelta.data);
    }
    if (
      update.rawOutput &&
      typeof update.rawOutput === "object" &&
      typeof update.rawOutput.formatted_output === "string"
    ) {
      existing.terminalOutput = appendCapped("", update.rawOutput.formatted_output);
    }
    if (metadata.terminal_exit && typeof metadata.terminal_exit === "object") {
      const exitCode = metadata.terminal_exit.exit_code;
      if (Number.isInteger(exitCode) && !Object.hasOwn(update, "status")) {
        existing.status = exitCode === 0 ? "completed" : "failed";
      }
    }
    if (
      update.rawOutput &&
      typeof update.rawOutput === "object" &&
      Number.isInteger(update.rawOutput.exit_code) &&
      !Object.hasOwn(update, "status")
    ) {
      existing.status = update.rawOutput.exit_code === 0 ? "completed" : "failed";
    }
    const sections = [existing.contentOutput, existing.terminalOutput].filter(Boolean);
    existing.output = sections.join("\n");
    this.tools.set(update.toolCallId, existing);
  }

  #setConfigValue(id, value) {
    if (typeof value !== "string") {
      return;
    }
    this.configOptions = this.configOptions.map((option) =>
      option.id === id ? { ...option, currentValue: value } : option,
    );
  }

  #trim() {
    let characters = this.messages.reduce(
      (total, message) => total + message.text.length,
      0,
    );
    characters += [...this.tools.values()].reduce(
      (total, tool) => total + tool.output.length,
      0,
    );
    while (
      this.messages.length + this.tools.size > this.maxItems ||
      characters > this.maxCharacters
    ) {
      const firstMessage = this.messages[0];
      const firstTool = this.tools.values().next().value;
      if (!firstTool || (firstMessage && firstMessage.sequence < firstTool.sequence)) {
        const removed = this.messages.shift();
        characters -= removed ? removed.text.length : 0;
      } else {
        this.tools.delete(firstTool.id);
        characters -= firstTool.output.length;
      }
    }
  }

  snapshot({ canPrompt = true } = {}) {
    return {
      sessionId: this.sessionId,
      status: this.status,
      statusText: this.statusText,
      busy: this.busy,
      canCancelTurn: this.turnInProgress && !this.cancelRequested,
      canPrompt,
      error: this.error,
      messages: this.messages.map(
        ({ localEcho: _localEcho, suppressEcho: _suppressEcho, ...message }) => message,
      ),
      tools: [...this.tools.values()].map(
        ({ contentOutput: _contentOutput, terminalOutput: _terminalOutput, ...tool }) =>
          tool,
      ),
      plan: this.plan,
      configOptions: this.configOptions,
      title: this.title,
      usage: this.usage,
    };
  }
}

module.exports = {
  SessionState,
  appendCapped,
  contentText,
  DEFAULT_MAX_CHARACTERS,
  toolContentText,
};
