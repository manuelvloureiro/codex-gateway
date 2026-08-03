"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { AcpClient } = require("../src/acp-client");

function initialized({ additionalDirectories = true } = {}) {
  return {
    protocolVersion: 1,
    agentInfo: { name: "test-agent", version: "1.0.0" },
    agentCapabilities: {
      loadSession: true,
      sessionCapabilities: {
        close: {},
        ...(additionalDirectories ? { additionalDirectories: {} } : {}),
      },
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}

class StubPeer {
  constructor(handler = () => ({})) {
    this.handler = handler;
    this.requests = [];
    this.notifications = [];
    this.disposals = [];
  }

  request(method, params, options) {
    this.requests.push({ method, params, options });
    return Promise.resolve().then(() => this.handler(method, params, options));
  }

  notify(method, params) {
    this.notifications.push({ method, params });
  }

  dispose(reason) {
    this.disposals.push(reason);
  }
}

test("initialize negotiates ACP v1 with filesystem and terminal callbacks disabled", async () => {
  const peer = new StubPeer((method) => {
    assert.equal(method, "initialize");
    return initialized();
  });
  const client = new AcpClient(peer, {
    clientInfo: { name: "unit-client", title: "Unit Client", version: "2.0.0" },
  });

  const result = await client.initialize();

  assert.equal(result.protocolVersion, 1);
  assert.deepEqual(peer.requests[0].params, {
    protocolVersion: 1,
    clientCapabilities: {
      fs: { readTextFile: false, writeTextFile: false },
      terminal: false,
    },
    clientInfo: { name: "unit-client", title: "Unit Client", version: "2.0.0" },
  });
  assert.equal(peer.requests[0].options.timeoutMs, 120_000);
});

test("session/new includes additional roots only when the agent advertises support", async (t) => {
  await t.test("supported", async () => {
    const peer = new StubPeer((method) => {
      if (method === "initialize") {
        return initialized();
      }
      return { sessionId: "session-supported" };
    });
    const client = new AcpClient(peer);

    await client.newSession({
      cwd: "/workspace/one",
      mcpServers: [],
      additionalDirectories: ["/workspace/two"],
    });

    assert.deepEqual(peer.requests[1], {
      method: "session/new",
      params: {
        cwd: "/workspace/one",
        mcpServers: [],
        additionalDirectories: ["/workspace/two"],
      },
      options: { timeoutMs: 120_000 },
    });
  });

  await t.test("not supported", async () => {
    const peer = new StubPeer((method) => {
      if (method === "initialize") {
        return initialized({ additionalDirectories: false });
      }
      return { sessionId: "session-basic" };
    });
    const client = new AcpClient(peer);

    await client.newSession({
      cwd: "/workspace/one",
      additionalDirectories: ["/workspace/two"],
    });

    assert.deepEqual(peer.requests[1].params, {
      cwd: "/workspace/one",
      mcpServers: [],
    });
  });
});

test("session/new buffers early updates until the caller can install returned session state", async () => {
  const updates = [];
  let sessionInstalled = false;
  let client;
  const peer = new StubPeer((method) => {
    if (method === "initialize") {
      return initialized();
    }
    client.handleNotification({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "session-early",
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "early" },
        },
      },
    });
    return { sessionId: "session-early" };
  });
  client = new AcpClient(peer, {
    onSessionUpdate: (sessionId, update) => {
      assert.equal(sessionInstalled, true);
      updates.push({ sessionId, update });
    },
  });

  const session = await client.newSession({ cwd: "/workspace" });
  assert.equal(session.sessionId, "session-early");
  assert.deepEqual(updates, []);

  sessionInstalled = true;
  await nextTurn();
  assert.deepEqual(updates, [
    {
      sessionId: "session-early",
      update: {
        sessionUpdate: "agent_message_chunk",
        content: { type: "text", text: "early" },
      },
    },
  ]);
});

test("prompt streams session/update notifications while its request is pending", async () => {
  const updates = [];
  let client;
  const peer = new StubPeer((method, params) => {
    assert.equal(method, "session/prompt");
    client.handleNotification({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: params.sessionId,
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "streamed" },
        },
      },
    });
    return { stopReason: "end_turn" };
  });
  client = new AcpClient(peer, {
    onSessionUpdate: (sessionId, update) => updates.push({ sessionId, update }),
  });

  const result = await client.prompt("session-1", "hello");

  assert.deepEqual(peer.requests[0].params, {
    sessionId: "session-1",
    prompt: [{ type: "text", text: "hello" }],
  });
  assert.equal(peer.requests[0].options.timeoutMs, 0);
  assert.deepEqual(updates, [
    {
      sessionId: "session-1",
      update: {
        sessionUpdate: "agent_message_chunk",
        content: { type: "text", text: "streamed" },
      },
    },
  ]);
  assert.deepEqual(result, { stopReason: "end_turn" });
});

test("permission requests return the exact ACP selected outcome", async () => {
  const peer = new StubPeer();
  let response;
  let rpcError;
  const requestParams = {
    sessionId: "session-1",
    toolCall: { toolCallId: "tool-1", kind: "execute" },
    options: [
      { optionId: "allow_once", name: "Allow Once", kind: "allow_once" },
      { optionId: "reject_once", name: "Reject", kind: "reject_once" },
    ],
  };
  const client = new AcpClient(peer, {
    onPermissionRequest: async (params) => {
      assert.equal(params, requestParams);
      return { outcome: "selected", optionId: "allow_once" };
    },
  });

  await client.handleRequest({
    id: 91,
    method: "session/request_permission",
    params: requestParams,
    respond: (value) => {
      response = value;
    },
    error: (...args) => {
      rpcError = args;
    },
  });

  assert.deepEqual(response, {
    outcome: { outcome: "selected", optionId: "allow_once" },
  });
  assert.equal(rpcError, undefined);
});

test("permission response write failures reject the request handler instead of leaking", async () => {
  const peer = new StubPeer();
  const writeError = new Error("agent stdin closed");
  const client = new AcpClient(peer, {
    onPermissionRequest: () => "allow_once",
  });

  await assert.rejects(
    client.handleRequest({
      id: "permission-write-error",
      method: "session/request_permission",
      params: {
        sessionId: "session-1",
        toolCall: { toolCallId: "tool-1" },
        options: [{ optionId: "allow_once", name: "Allow", kind: "allow_once" }],
      },
      respond: () => Promise.reject(writeError),
      error: () => assert.fail("selected permission should not send an error response"),
    }),
    (error) => error === writeError,
  );
});

test("cancel sends a notification and cancels outstanding permission requests", async () => {
  const decision = deferred();
  const peer = new StubPeer();
  let response;
  const client = new AcpClient(peer, {
    onPermissionRequest: () => decision.promise,
  });
  const handling = client.handleRequest({
    id: "permission-1",
    method: "session/request_permission",
    params: {
      sessionId: "session-1",
      toolCall: { toolCallId: "tool-1" },
      options: [{ optionId: "allow_once", name: "Allow", kind: "allow_once" }],
    },
    respond: (value) => {
      response = value;
    },
    error: () => assert.fail("permission request should not fail"),
  });
  await nextTurn();

  client.cancel("session-1");

  assert.deepEqual(peer.notifications, [
    { method: "session/cancel", params: { sessionId: "session-1" } },
  ]);
  assert.deepEqual(response, { outcome: { outcome: "cancelled" } });
  decision.resolve("allow_once");
  await handling;
  assert.deepEqual(response, { outcome: { outcome: "cancelled" } });
});

test("closeSession closes advertised sessions and skips unsupported agents", async (t) => {
  await t.test("supported", async () => {
    const peer = new StubPeer((method) => {
      if (method === "initialize") {
        return initialized();
      }
      return {};
    });
    const client = new AcpClient(peer);

    assert.equal(await client.closeSession("session-1"), true);
    assert.deepEqual(peer.requests[1], {
      method: "session/close",
      params: { sessionId: "session-1" },
      options: { timeoutMs: 120_000 },
    });
  });

  await t.test("unsupported", async () => {
    const peer = new StubPeer(() => ({
      ...initialized(),
      agentCapabilities: { sessionCapabilities: {} },
    }));
    const client = new AcpClient(peer);

    assert.equal(await client.closeSession("session-1"), false);
    assert.equal(peer.requests.length, 1);
  });
});

test("setConfigOption and setMode use their exact ACP request shapes", async () => {
  const peer = new StubPeer((method, params) => ({ method, params }));
  const client = new AcpClient(peer);

  await client.setConfigOption("session-1", "model", "gpt-5.6-sol");
  await client.setConfigOption("session-1", "fast-mode", true, "boolean");
  await client.setMode("session-1", "agent");

  assert.deepEqual(
    peer.requests.map(({ method, params }) => ({ method, params })),
    [
      {
        method: "session/set_config_option",
        params: { sessionId: "session-1", configId: "model", value: "gpt-5.6-sol" },
      },
      {
        method: "session/set_config_option",
        params: {
          sessionId: "session-1",
          configId: "fast-mode",
          value: true,
          type: "boolean",
        },
      },
      {
        method: "session/set_mode",
        params: { sessionId: "session-1", modeId: "agent" },
      },
    ],
  );
});

test("unknown inbound requests receive JSON-RPC method-not-found", async () => {
  const peer = new StubPeer();
  let errorArgs;
  const client = new AcpClient(peer);

  await client.handleRequest({
    id: "unknown-1",
    method: "workspace/do_something",
    params: {},
    respond: () => assert.fail("unknown request should not receive a result"),
    error: (...args) => {
      errorArgs = args;
    },
  });

  assert.deepEqual(errorArgs, [
    -32601,
    "Method not found",
    { method: "workspace/do_something" },
  ]);
});

test("prompts are serialized per session but independent across sessions", async () => {
  const pending = [];
  const peer = new StubPeer((method, params) => {
    assert.equal(method, "session/prompt");
    const item = deferred();
    pending.push({ params, ...item });
    return item.promise;
  });
  const client = new AcpClient(peer);

  const first = client.prompt("session-1", "first");
  const second = client.prompt("session-1", "second");
  const other = client.prompt("session-2", "other");
  await nextTurn();

  assert.equal(pending.length, 2);
  assert.deepEqual(
    pending.map((item) => item.params.prompt[0].text),
    ["first", "other"],
  );

  pending[0].resolve({ stopReason: "end_turn" });
  await first;
  await nextTurn();
  assert.equal(pending.length, 3);
  assert.equal(pending[2].params.prompt[0].text, "second");

  pending[1].resolve({ stopReason: "end_turn" });
  pending[2].resolve({ stopReason: "end_turn" });
  await Promise.all([second, other]);
});

test("process and JSON-RPC request errors propagate and do not poison the prompt queue", async () => {
  const rpcError = Object.assign(new Error("Authentication required"), {
    code: -32000,
    data: { login: true },
  });
  let promptCount = 0;
  const peer = new StubPeer((method) => {
    if (method === "initialize") {
      throw rpcError;
    }
    promptCount += 1;
    if (promptCount === 1) {
      throw new Error("agent process exited with code 23");
    }
    return { stopReason: "end_turn" };
  });
  const client = new AcpClient(peer);

  await assert.rejects(client.initialize(), (error) => {
    assert.equal(error, rpcError);
    assert.equal(error.code, -32000);
    return true;
  });

  const failed = client.prompt("session-1", "first");
  const recovered = client.prompt("session-1", "second");
  await assert.rejects(failed, /process exited with code 23/);
  assert.deepEqual(await recovered, { stopReason: "end_turn" });
});

test("component smoke uses the real child-process NDJSON peer", async (t) => {
  let spawnPeer;
  try {
    ({ spawnPeer } = require("../src/ndjson-rpc"));
  } catch (error) {
    if (error && error.code === "MODULE_NOT_FOUND") {
      t.skip("ndjson-rpc component is not available yet");
      return;
    }
    throw error;
  }

  const updates = [];
  const peer = spawnPeer(
    process.execPath,
    [path.join(__dirname, "fixtures", "fake-agent.js")],
  );
  const client = new AcpClient(peer, {
    onSessionUpdate: (sessionId, update) => updates.push({ sessionId, update }),
    onPermissionRequest: () => ({ outcome: "selected", optionId: "allow_once" }),
  });
  t.after(() => client.dispose());

  const initializeResult = await client.initialize();
  const session = await client.newSession({
    cwd: process.cwd(),
    mcpServers: [],
    additionalDirectories: [path.dirname(process.cwd())],
  });
  const promptResult = await client.prompt(session.sessionId, "smoke test");

  assert.equal(initializeResult.protocolVersion, 1);
  assert.match(session.sessionId, /^fake-session-/);
  assert.equal(promptResult.stopReason, "end_turn");
  assert.equal(
    updates.map(({ update }) => update.content.text).join(""),
    "hello from fake agent",
  );
});
