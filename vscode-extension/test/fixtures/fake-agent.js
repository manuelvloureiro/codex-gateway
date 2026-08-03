#!/usr/bin/env node
"use strict";

const readline = require("node:readline");

let sessionSequence = 0;
let permissionSequence = 0;
const prompts = new Map();
const permissions = new Map();

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function respond(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function fail(id, code, message, data) {
  send({
    jsonrpc: "2.0",
    id,
    error: { code, message, ...(data === undefined ? {} : { data }) },
  });
}

function update(sessionId, value) {
  send({
    jsonrpc: "2.0",
    method: "session/update",
    params: { sessionId, update: value },
  });
}

function finishPrompt(promptId, stopReason = "end_turn") {
  const prompt = prompts.get(promptId);
  if (!prompt || prompt.finished) {
    return;
  }
  prompt.finished = true;
  prompts.delete(promptId);
  respond(promptId, {
    stopReason,
    usage: {
      totalTokens: 12,
      inputTokens: 7,
      cachedReadTokens: 0,
      outputTokens: 5,
      thoughtTokens: 0,
    },
  });
}

function handlePermissionResponse(message) {
  const pending = permissions.get(message.id);
  if (!pending) {
    return false;
  }
  permissions.delete(message.id);
  const prompt = prompts.get(pending.promptId);
  if (!prompt || prompt.finished) {
    return true;
  }
  const outcome = message.result && message.result.outcome;
  if (outcome && outcome.outcome === "cancelled") {
    finishPrompt(pending.promptId, "cancelled");
    return true;
  }
  if (!outcome || outcome.outcome !== "selected" || outcome.optionId !== "allow_once") {
    prompts.delete(pending.promptId);
    fail(pending.promptId, -32000, "Unexpected permission response", message);
    return true;
  }
  update(prompt.sessionId, {
    sessionUpdate: "agent_message_chunk",
    messageId: `assistant-${String(pending.promptId)}`,
    content: { type: "text", text: "from fake agent" },
  });
  finishPrompt(pending.promptId);
  return true;
}

function handleRequest(message) {
  switch (message.method) {
    case "initialize":
      respond(message.id, {
        protocolVersion: 1,
        agentInfo: { name: "fake-acp-agent", version: "1.0.0" },
        agentCapabilities: {
          loadSession: true,
          promptCapabilities: { image: false, embeddedContext: false },
          sessionCapabilities: { additionalDirectories: {} },
        },
      });
      break;

    case "session/new": {
      const sessionId = `fake-session-${++sessionSequence}`;
      respond(message.id, {
        sessionId,
        configOptions: [
          {
            id: "model",
            name: "Model",
            category: "model",
            type: "select",
            currentValue: "fake-model",
            options: [{ value: "fake-model", name: "Fake Model" }],
          },
        ],
      });
      break;
    }

    case "session/load":
      update(message.params.sessionId, {
        sessionUpdate: "agent_message_chunk",
        messageId: "loaded-message",
        content: { type: "text", text: "loaded" },
      });
      respond(message.id, { configOptions: [] });
      break;

    case "session/prompt": {
      const sessionId = message.params.sessionId;
      prompts.set(message.id, { sessionId, finished: false });
      update(sessionId, {
        sessionUpdate: "agent_message_chunk",
        messageId: `assistant-${String(message.id)}`,
        content: { type: "text", text: "hello " },
      });
      const permissionId = `permission-${++permissionSequence}`;
      permissions.set(permissionId, { promptId: message.id });
      send({
        jsonrpc: "2.0",
        id: permissionId,
        method: "session/request_permission",
        params: {
          sessionId,
          toolCall: {
            toolCallId: `tool-${permissionSequence}`,
            kind: "execute",
            status: "pending",
            rawInput: { command: "echo fake", cwd: message.params.cwd || process.cwd() },
          },
          options: [
            { optionId: "allow_once", name: "Allow Once", kind: "allow_once" },
            { optionId: "reject_once", name: "Reject", kind: "reject_once" },
          ],
        },
      });
      break;
    }

    case "session/set_config_option":
      respond(message.id, {
        configOptions: [
          {
            id: message.params.configId,
            name: message.params.configId,
            type: typeof message.params.value === "boolean" ? "boolean" : "select",
            currentValue: message.params.value,
          },
        ],
      });
      break;

    case "session/set_mode":
      respond(message.id, {});
      break;

    case "fake/invalid_json":
      process.stdout.write("this is not json\n");
      break;

    case "fake/exit":
      process.exitCode = 23;
      process.stdin.destroy();
      break;

    case "fake/respond_and_exit":
      process.stdout.write(
        `${JSON.stringify({ jsonrpc: "2.0", id: message.id, result: "final response" })}\n`,
        () => process.exit(0),
      );
      break;

    default:
      fail(message.id, -32601, "Method not found", { method: message.method });
      break;
  }
}

function handleNotification(message) {
  if (message.method !== "session/cancel") {
    return;
  }
  for (const [promptId, prompt] of prompts) {
    if (prompt.sessionId === message.params.sessionId) {
      finishPrompt(promptId, "cancelled");
    }
  }
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    process.stderr.write(`invalid request: ${error.message}\n`);
    return;
  }

  if (!message.method && Object.prototype.hasOwnProperty.call(message, "id")) {
    handlePermissionResponse(message);
    return;
  }
  if (Object.prototype.hasOwnProperty.call(message, "id")) {
    handleRequest(message);
  } else {
    handleNotification(message);
  }
});
