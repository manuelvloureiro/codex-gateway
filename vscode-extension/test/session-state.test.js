"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  SessionState,
  appendCapped,
} = require("../src/session-state");

function textUpdate(sessionUpdate, text, messageId) {
  return {
    sessionUpdate,
    ...(messageId ? { messageId } : {}),
    content: { type: "text", text },
  };
}

test("groups message chunks by message id and adjacent id-less role", () => {
  const state = new SessionState();
  state.startSession({ sessionId: "session-1" });
  state.beginPrompt("Run the checks");

  // The ACP echo acknowledges the optimistic local message instead of duplicating it.
  state.applyUpdate(textUpdate("user_message_chunk", "Run ", "prompt-1"));
  state.applyUpdate(textUpdate("user_message_chunk", "the checks", "prompt-1"));
  state.applyUpdate(textUpdate("agent_message_chunk", "First ", "reply-1"));
  state.applyUpdate(textUpdate("agent_message_chunk", "reply", "reply-1"));
  state.applyUpdate(textUpdate("agent_message_chunk", "Second reply", "reply-2"));
  state.applyUpdate(textUpdate("agent_thought_chunk", "Checking "));
  state.applyUpdate(textUpdate("agent_thought_chunk", "files"));
  state.applyUpdate({
    sessionUpdate: "agent_message_chunk",
    content: { type: "unknown", value: "ignored" },
  });

  const { messages } = state.snapshot();
  assert.equal(messages.length, 4);
  assert.deepEqual(
    messages.map(({ id, role, text }) => ({ id, role, text })),
    [
      { id: "prompt-1", role: "user", text: "Run the checks" },
      { id: "reply-1", role: "agent", text: "First reply" },
      { id: "reply-2", role: "agent", text: "Second reply" },
      { id: messages[3].id, role: "thought", text: "Checking files" },
    ],
  );
});

test("merges partial tool updates while treating content as an authoritative snapshot", () => {
  const state = new SessionState();
  state.applyUpdate({
    sessionUpdate: "tool_call",
    toolCallId: "tool-1",
    title: "Edit file",
    kind: "edit",
    status: "in_progress",
    content: [
      { type: "content", content: { type: "text", text: "Preparing edit" } },
      {
        type: "diff",
        path: "src/index.js",
        oldText: "old",
        newText: "new",
      },
    ],
  });
  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "tool-1",
    status: "completed",
  });

  let tool = state.snapshot().tools[0];
  assert.equal(tool.title, "Edit file");
  assert.equal(tool.kind, "edit");
  assert.equal(tool.status, "completed");
  assert.equal(
    tool.output,
    "Preparing edit\nDiff: src/index.js\n--- before\nold\n+++ after\nnew",
  );

  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "tool-1",
    content: [{ type: "content", content: { type: "text", text: "Edit saved" } }],
  });

  tool = state.snapshot().tools[0];
  assert.equal(tool.output, "Edit saved");
  assert.equal(Object.hasOwn(tool, "contentOutput"), false);
  assert.equal(Object.hasOwn(tool, "terminalOutput"), false);
});

test("appends terminal deltas and replaces them with authoritative rawOutput", () => {
  const state = new SessionState();
  state.applyUpdate({
    sessionUpdate: "tool_call",
    toolCallId: "shell-raw",
    title: "Run tests",
    content: [{ type: "content", content: { type: "text", text: "Command output" } }],
  });
  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "shell-raw",
    _meta: { terminal_output_delta: "partial " },
  });
  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "shell-raw",
    _meta: { terminal_output_delta: { data: "line" } },
  });

  assert.equal(state.snapshot().tools[0].output, "Command output\npartial line");

  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "shell-raw",
    rawOutput: {
      formatted_output: "complete raw output\n",
      exit_code: 0,
    },
  });

  const tool = state.snapshot().tools[0];
  assert.equal(tool.output, "Command output\ncomplete raw output\n");
  assert.equal(tool.status, "completed");
});

test("appends metadata terminal output chunks and honors explicit status", () => {
  const state = new SessionState();
  state.applyUpdate({
    sessionUpdate: "tool_call",
    toolCallId: "shell-meta",
    status: "in_progress",
    _meta: { terminal_output_delta: "stale partial" },
  });
  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "shell-meta",
    _meta: {
      terminal_output: { data: "complete metadata output" },
      terminal_exit: { exit_code: 7 },
    },
  });

  let tool = state.snapshot().tools[0];
  assert.equal(tool.output, "stale partialcomplete metadata output");
  assert.equal(tool.status, "failed");

  state.applyUpdate({
    sessionUpdate: "tool_call_update",
    toolCallId: "shell-meta",
    status: "completed",
    rawOutput: { formatted_output: "final", exit_code: 9 },
  });
  tool = state.snapshot().tools[0];
  assert.equal(tool.output, "final");
  assert.equal(tool.status, "completed");
});

test("applies plan, configuration, session-info, and usage updates", () => {
  const state = new SessionState();
  state.startSession({
    sessionId: "session-1",
    configOptions: [
      { id: "mode", name: "Mode", currentValue: "read-only" },
      { id: "model", name: "Model", currentValue: "gpt-5" },
      null,
      { name: "Missing id" },
    ],
  });

  state.applyUpdate({
    sessionUpdate: "plan",
    entries: [
      { content: "Inspect", status: "completed", priority: "high" },
      { content: "Implement", status: "in_progress", priority: "high" },
    ],
  });
  state.applyUpdate({ sessionUpdate: "current_mode_update", currentModeId: "agent" });
  state.applyUpdate({ sessionUpdate: "session_info_update", title: "ACP session" });
  state.applyUpdate({ sessionUpdate: "usage_update", used: 12, size: 100 });

  let snapshot = state.snapshot();
  assert.equal(snapshot.plan.length, 2);
  assert.deepEqual(
    snapshot.configOptions.map(({ id, currentValue }) => ({ id, currentValue })),
    [
      { id: "mode", currentValue: "agent" },
      { id: "model", currentValue: "gpt-5" },
    ],
  );
  assert.equal(snapshot.title, "ACP session");
  assert.deepEqual(snapshot.usage, {
    sessionUpdate: "usage_update",
    used: 12,
    size: 100,
  });

  state.applyUpdate({
    sessionUpdate: "session_info_update",
    title: null,
    _meta: {
      codex: {
        error: { message: "Provider disconnected", willRetry: true },
      },
    },
  });
  snapshot = state.snapshot();
  assert.equal(snapshot.title, "");
  assert.equal(snapshot.statusText, "Codex is retrying — Provider disconnected");

  state.applyUpdate({ sessionUpdate: "plan_removed" });
  state.applyUpdate({
    sessionUpdate: "config_option_update",
    configOptions: [{ id: "model", currentValue: "gpt-5.6-sol" }, { nope: true }],
  });
  snapshot = state.snapshot();
  assert.deepEqual(snapshot.plan, []);
  assert.deepEqual(snapshot.configOptions, [
    { id: "model", currentValue: "gpt-5.6-sol" },
  ]);
});

test("bounds the combined message and tool history by oldest sequence", () => {
  const state = new SessionState({ maxItems: 3 });
  state.applyUpdate(textUpdate("agent_message_chunk", "message one", "message-1"));
  state.applyUpdate({
    sessionUpdate: "tool_call",
    toolCallId: "tool-1",
    title: "First tool",
  });
  state.applyUpdate(textUpdate("agent_message_chunk", "message two", "message-2"));
  state.applyUpdate({
    sessionUpdate: "tool_call",
    toolCallId: "tool-2",
    title: "Second tool",
  });

  let snapshot = state.snapshot();
  assert.deepEqual(snapshot.messages.map((message) => message.id), ["message-2"]);
  assert.deepEqual(snapshot.tools.map((tool) => tool.id), ["tool-1", "tool-2"]);

  state.applyUpdate(textUpdate("agent_message_chunk", "message three", "message-3"));
  snapshot = state.snapshot();
  assert.deepEqual(snapshot.messages.map((message) => message.id), [
    "message-2",
    "message-3",
  ]);
  assert.deepEqual(snapshot.tools.map((tool) => tool.id), ["tool-2"]);
  assert.equal(snapshot.messages.length + snapshot.tools.length, 3);
});

test("bounds total rendered transcript characters by oldest sequence", () => {
  const state = new SessionState({ maxItems: 20, maxCharacters: 20 });
  state.applyUpdate(textUpdate("agent_message_chunk", "1234567890", "message-1"));
  state.applyUpdate({
    sessionUpdate: "tool_call",
    toolCallId: "tool-1",
    content: [{ type: "content", content: { type: "text", text: "abcdefghij" } }],
  });
  state.applyUpdate(textUpdate("agent_message_chunk", "newest", "message-2"));

  const snapshot = state.snapshot();
  assert.deepEqual(snapshot.messages.map((message) => message.id), ["message-2"]);
  assert.deepEqual(snapshot.tools.map((tool) => tool.id), ["tool-1"]);
});

test("caps accumulated text while retaining the newest output", () => {
  const newest = "newest output";
  const output = appendCapped("prefix", `${"x".repeat(200_000)}${newest}`);

  assert.match(output, /^\[earlier output truncated\]\n/);
  assert.ok(output.endsWith(newest));
  assert.equal(output.length, 200_000 + "[earlier output truncated]\n".length);
});

test("tracks prompt completion and errors, and resets all transient state", () => {
  const state = new SessionState();
  assert.deepEqual(
    (({ sessionId, status, statusText, busy, error }) => ({
      sessionId,
      status,
      statusText,
      busy,
      error,
    }))(state.snapshot()),
    {
      sessionId: null,
      status: "idle",
      statusText: "Not connected",
      busy: false,
      error: "",
    },
  );

  state.startSession({ sessionId: "session-1", configOptions: [{ id: "mode" }] });
  state.beginPrompt("Do work");
  assert.equal(state.snapshot().busy, true);
  assert.equal(state.snapshot().status, "busy");
  assert.equal(state.snapshot().canCancelTurn, true);

  assert.equal(state.markCancelling(), true);
  assert.equal(state.snapshot().canCancelTurn, false);
  assert.equal(state.markCancelling(), false);

  state.completePrompt("max_tokens");
  assert.equal(state.snapshot().busy, false);
  assert.equal(state.snapshot().status, "ready");
  assert.equal(state.snapshot().statusText, "Ready — max tokens");
  assert.equal(state.snapshot().canCancelTurn, false);

  state.setError(new Error("transport closed"));
  assert.equal(state.snapshot().busy, false);
  assert.equal(state.snapshot().status, "error");
  assert.equal(state.snapshot().statusText, "ACP error");
  assert.equal(state.snapshot().error, "transport closed");

  state.applyUpdate({ sessionUpdate: "plan", entries: [{ content: "stale" }] });
  state.applyUpdate({ sessionUpdate: "tool_call", toolCallId: "stale-tool" });
  state.startSession({
    sessionId: "session-2",
    configOptions: [{ id: "model", currentValue: "gpt-5.6-sol" }],
  });

  const snapshot = state.snapshot({ canPrompt: false });
  assert.equal(snapshot.sessionId, "session-2");
  assert.equal(snapshot.status, "ready");
  assert.equal(snapshot.statusText, "Ready");
  assert.equal(snapshot.busy, false);
  assert.equal(snapshot.canPrompt, false);
  assert.equal(snapshot.error, "");
  assert.deepEqual(snapshot.messages, []);
  assert.deepEqual(snapshot.tools, []);
  assert.deepEqual(snapshot.plan, []);
  assert.deepEqual(snapshot.configOptions, [
    { id: "model", currentValue: "gpt-5.6-sol" },
  ]);
  assert.equal(snapshot.title, "");
  assert.equal(snapshot.usage, null);
});
