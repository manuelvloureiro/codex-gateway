"use strict";

const assert = require("node:assert/strict");
const Module = require("node:module");
const test = require("node:test");

const settings = new Map([
  ["launcherPath", "/fake/codex-gateway-acp"],
  ["launcherArgs", []],
  ["gatewayUrl", "http://127.0.0.1:8085/v1"],
  ["model", "gpt-test"],
  ["initialMode", "read-only"],
  ["adapterPath", ""],
  ["workingDirectory", ""],
  ["permissionPolicy", "deny"],
  ["logProtocol", false],
]);

const vscode = {
  workspace: {
    isTrusted: true,
    workspaceFolders: [
      { uri: { scheme: "file", fsPath: "/workspace/one" } },
    ],
    getConfiguration: () => ({
      get: (key, fallback) => settings.has(key) ? settings.get(key) : fallback,
    }),
  },
  window: {
    showQuickPick: async () => undefined,
  },
  Uri: {
    joinPath: (base, ...parts) => ({ base, parts }),
  },
  CancellationTokenSource: class {
    constructor() {
      this.token = {};
    }

    cancel() {}

    dispose() {}
  },
};

const originalLoad = Module._load;
Module._load = function loadWithVscodeStub(request, parent, isMain) {
  if (request === "vscode") {
    return vscode;
  }
  return originalLoad.call(this, request, parent, isMain);
};
const {
  CodexGatewayViewProvider,
  sessionPaths,
} = require("../src/extension");
Module._load = originalLoad;

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

async function eventually(predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) {
      return;
    }
    await nextTurn();
  }
  assert.fail("condition did not become true");
}

class FakePeer {
  constructor() {
    this.listeners = new Map();
    this.disposals = [];
  }

  on(event, listener) {
    this.listeners.set(event, listener);
  }

  emit(event, value) {
    const listener = this.listeners.get(event);
    if (listener) {
      listener(value);
    }
  }

  dispose(reason) {
    this.disposals.push(reason);
  }
}

function providerFixture() {
  const peers = [];
  const clients = [];
  let sessionSequence = 0;

  class FakeClient {
    constructor(peer, options) {
      this.peer = peer;
      this.options = options;
      this.promptCalls = [];
      this.closeCalls = [];
      this.disposals = [];
      clients.push(this);
    }

    async initialize(options) {
      this.initializeOptions = options;
      return {
        protocolVersion: 1,
        agentInfo: { name: "fake-agent", version: "1.0.0" },
        agentCapabilities: { sessionCapabilities: { close: {} } },
      };
    }

    async newSession(params) {
      this.newSessionParams = params;
      return { sessionId: `session-${++sessionSequence}`, configOptions: [] };
    }

    prompt(sessionId, content) {
      const pending = deferred();
      this.promptCalls.push({ sessionId, content, pending });
      return pending.promise;
    }

    async closeSession(sessionId) {
      this.closeCalls.push(sessionId);
      return true;
    }

    async cancel() {}

    async setConfigOption() {
      return {};
    }

    dispose(reason) {
      this.disposals.push(reason);
    }
  }

  const output = {
    lines: [],
    append() {},
    appendLine(line) {
      this.lines.push(line);
    },
    show() {},
  };
  const provider = new CodexGatewayViewProvider(
    { extension: { packageJSON: { version: "0.1.0" } } },
    output,
    {
      Client: FakeClient,
      createPeer: () => {
        const peer = new FakePeer();
        peers.push(peer);
        return peer;
      },
    },
  );
  return { clients, output, peers, provider };
}

test("sessionPaths includes every workspace root except the configured cwd", () => {
  const originalFolders = vscode.workspace.workspaceFolders;
  const originalWorkingDirectory = settings.get("workingDirectory");
  vscode.workspace.workspaceFolders = [
    { uri: { scheme: "file", fsPath: "/workspace/one" } },
    { uri: { scheme: "file", fsPath: "/workspace/two" } },
    { uri: { scheme: "file", fsPath: "/workspace/three" } },
    { uri: { scheme: "untitled", fsPath: "/ignored" } },
  ];
  settings.set("workingDirectory", "/workspace/two");
  try {
    assert.deepEqual(
      sessionPaths({ get: (key, fallback) => settings.get(key) ?? fallback }),
      {
        cwd: "/workspace/two",
        additionalDirectories: ["/workspace/one", "/workspace/three"],
      },
    );
  } finally {
    vscode.workspace.workspaceFolders = originalFolders;
    settings.set("workingDirectory", originalWorkingDirectory);
  }
});

test("a restart creates a new client and stale prompt completion cannot overwrite it", async () => {
  const { clients, provider } = providerFixture();
  const firstPrompt = provider.prompt("first turn");
  await eventually(() => clients[0] && clients[0].promptCalls.length === 1);
  const stalePrompt = clients[0].promptCalls[0];

  await provider.restart();
  assert.equal(clients.length, 2);
  assert.equal(provider.state.snapshot().sessionId, "session-2");
  assert.equal(clients[1].initializeOptions.timeoutMs, 300_000);

  stalePrompt.pending.resolve({ stopReason: "max_tokens" });
  await firstPrompt;
  assert.equal(provider.state.snapshot().sessionId, "session-2");
  assert.equal(provider.state.snapshot().statusText, "Ready");
  provider.dispose();
});

test("a prompt after an unexpected child exit reconnects instead of being dropped", async () => {
  const { clients, peers, provider } = providerFixture();
  await provider.newSession();
  peers[0].emit("close", new Error("child exited"));
  assert.equal(provider.state.snapshot().status, "error");

  const retry = provider.prompt("retry this task");
  await eventually(() => clients[1] && clients[1].promptCalls.length === 1);
  clients[1].promptCalls[0].pending.resolve({ stopReason: "end_turn" });
  await retry;

  assert.equal(clients.length, 2);
  assert.equal(provider.state.snapshot().sessionId, "session-2");
  assert.equal(provider.state.snapshot().statusText, "Ready — end turn");
  provider.dispose();
});

test("creating a replacement session closes the previous idle session", async () => {
  const { clients, provider } = providerFixture();
  await provider.newSession();
  await provider.newSession();
  await nextTurn();

  assert.deepEqual(clients[0].closeCalls, ["session-1"]);
  assert.equal(provider.state.snapshot().sessionId, "session-2");
  provider.dispose();
});
