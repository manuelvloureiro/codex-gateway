'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const test = require('node:test');

const {
  JsonRpcError,
  JsonRpcProtocolError,
  JsonRpcTimeoutError,
  JsonRpcTransportError,
  NdjsonRpcPeer,
  spawnPeer,
} = require('../src/ndjson-rpc');

function lineReader(stream) {
  let buffer = '';
  const values = [];
  const waiters = [];

  stream.setEncoding('utf8');
  stream.on('data', (chunk) => {
    buffer += chunk;
    for (;;) {
      const newline = buffer.indexOf('\n');
      if (newline === -1) {
        break;
      }
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (!line) {
        continue;
      }
      const value = JSON.parse(line);
      const waiter = waiters.shift();
      if (waiter) {
        waiter.resolve(value);
      } else {
        values.push(value);
      }
    }
  });

  return {
    next() {
      if (values.length > 0) {
        return Promise.resolve(values.shift());
      }
      return new Promise((resolve, reject) => waiters.push({ resolve, reject }));
    },
  };
}

function makePeer(options = {}) {
  const readable = new PassThrough();
  const writable = new PassThrough();
  const stderr = new PassThrough();
  const output = lineReader(writable);
  const peer = new NdjsonRpcPeer({
    readable,
    writable,
    stderr,
    defaultTimeoutMs: 1_000,
    ...options,
  });
  return { peer, readable, writable, stderr, output };
}

test('correlates out-of-order requests and accepts split/coalesced input', async (t) => {
  const { peer, readable, output } = makePeer();
  t.after(() => peer.dispose());

  const firstResult = peer.request('alpha', { n: 1 });
  const secondResult = peer.request('beta', { n: 2 });
  const first = await output.next();
  const second = await output.next();

  assert.deepEqual(first, {
    jsonrpc: '2.0',
    id: 1,
    method: 'alpha',
    params: { n: 1 },
  });
  assert.deepEqual(second, {
    jsonrpc: '2.0',
    id: 2,
    method: 'beta',
    params: { n: 2 },
  });

  readable.write('{"jsonrpc":"2.0","id":2,"result":"sec');
  readable.write('ond"}\n{"jsonrpc":"2.0","id":1,"result":"first"}\n');

  assert.equal(await secondResult, 'second');
  assert.equal(await firstResult, 'first');
});

test('turns remote JSON-RPC errors into JsonRpcError', async (t) => {
  const { peer, readable, output } = makePeer();
  t.after(() => peer.dispose());

  const result = peer.request('will/fail');
  const request = await output.next();
  readable.write(`${JSON.stringify({
    jsonrpc: '2.0',
    id: request.id,
    error: { code: -32001, message: 'Nope', data: { why: 'test' } },
  })}\n`);

  await assert.rejects(result, (error) => {
    assert.ok(error instanceof JsonRpcError);
    assert.equal(error.code, -32001);
    assert.equal(error.requestId, request.id);
    assert.deepEqual(error.data, { why: 'test' });
    return true;
  });
});

test('dispatches notifications and incoming requests with one-shot responders', async (t) => {
  const notifications = [];
  const { peer, readable, output } = makePeer({
    onNotification: (notification) => notifications.push(notification),
    onRequest: async (request) => {
      if (request.method === 'permission/ask') {
        await request.respond({ outcome: 'selected' });
        await assert.rejects(request.respond({ outcome: 'again' }), /already answered/);
        return undefined;
      }
      return { echoed: request.params };
    },
  });
  t.after(() => peer.dispose());

  readable.write(
    `${JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { text: 'hi' } })}\n` +
      `${JSON.stringify({ jsonrpc: '2.0', id: 'ask-1', method: 'permission/ask', params: {} })}\n` +
      `${JSON.stringify({ jsonrpc: '2.0', id: 'echo-1', method: 'echo', params: { value: 7 } })}\n`,
  );

  const permission = await output.next();
  const echo = await output.next();
  assert.deepEqual(permission, {
    jsonrpc: '2.0',
    id: 'ask-1',
    result: { outcome: 'selected' },
  });
  assert.deepEqual(echo, {
    jsonrpc: '2.0',
    id: 'echo-1',
    result: { echoed: { value: 7 } },
  });
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].method, 'session/update');
  assert.deepEqual(notifications[0].params, { text: 'hi' });
});

test('supports explicit incoming error responses and safe handler failures', async (t) => {
  const { peer, readable, output } = makePeer({
    onRequest: (request) => {
      if (request.method === 'known/failure') {
        return request.error(-32042, 'Rejected', { option: 1 });
      }
      throw new Error('sensitive implementation detail');
    },
  });
  t.after(() => peer.dispose());

  readable.write(
    `${JSON.stringify({ jsonrpc: '2.0', id: 11, method: 'known/failure' })}\n` +
      `${JSON.stringify({ jsonrpc: '2.0', id: 12, method: 'handler/crash' })}\n`,
  );

  assert.deepEqual(await output.next(), {
    jsonrpc: '2.0',
    id: 11,
    error: { code: -32042, message: 'Rejected', data: { option: 1 } },
  });
  assert.deepEqual(await output.next(), {
    jsonrpc: '2.0',
    id: 12,
    error: { code: -32603, message: 'Internal error' },
  });
});

test('answers an unhandled incoming request with method not found', async (t) => {
  const { peer, readable, output } = makePeer();
  t.after(() => peer.dispose());

  readable.write(`${JSON.stringify({ jsonrpc: '2.0', id: 19, method: 'missing' })}\n`);
  assert.deepEqual(await output.next(), {
    jsonrpc: '2.0',
    id: 19,
    error: { code: -32601, message: 'Method not found: missing' },
  });
});

test('times out one request without closing the peer', async (t) => {
  const { peer, readable, output } = makePeer({ defaultTimeoutMs: 20 });
  t.after(() => peer.dispose());

  const timedOut = peer.request('slow');
  await output.next();
  await assert.rejects(timedOut, (error) => {
    assert.ok(error instanceof JsonRpcTimeoutError);
    assert.equal(error.code, 'ETIMEDOUT');
    assert.equal(error.method, 'slow');
    return true;
  });
  assert.equal(peer.disposed, false);

  const healthy = peer.request('healthy', undefined, { timeoutMs: 200 });
  const request = await output.next();
  readable.write(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: true })}\n`);
  assert.equal(await healthy, true);
});

test('malformed and oversized frames close the transport and reject pending work', async (t) => {
  await t.test('malformed JSON', async () => {
    const { peer, readable, output } = makePeer();
    const pending = peer.request('pending');
    await output.next();
    readable.write('{not-json}\n');

    await assert.rejects(pending, JsonRpcProtocolError);
    assert.equal(peer.disposed, true);
    assert.match(peer.closeReason.message, /malformed/);
  });

  await t.test('oversized line', async () => {
    const { peer, readable, output } = makePeer({ maxLineBytes: 64 });
    const pending = peer.request('x');
    await output.next();
    readable.write('x'.repeat(65));

    await assert.rejects(pending, JsonRpcProtocolError);
    assert.equal(peer.disposed, true);
    assert.match(peer.closeReason.message, /exceeds/);
  });
});

test('process close rejects requests and stderr reaches hooks', async () => {
  class FakeChild extends EventEmitter {
    constructor() {
      super();
      this.stdin = new PassThrough();
      this.stdout = new PassThrough();
      this.stderr = new PassThrough();
      this.exitCode = null;
      this.signalCode = null;
      this.killCalls = 0;
    }

    kill() {
      this.killCalls += 1;
      return true;
    }
  }

  const child = new FakeChild();
  const stderr = [];
  const logs = [];
  const peer = NdjsonRpcPeer.fromChild(child, {
    defaultTimeoutMs: 1_000,
    onStderr: (text) => stderr.push(text),
    onLog: (_message, entry) => logs.push(entry),
  });
  const output = lineReader(child.stdin);
  const pending = peer.request('wait');
  await output.next();

  child.stderr.write('adapter diagnostic\n');
  child.exitCode = 7;
  child.emit('exit', 7, null);
  child.emit('close', 7, null);

  await assert.rejects(pending, (error) => {
    assert.ok(error instanceof JsonRpcTransportError);
    assert.equal(error.exitCode, 7);
    return true;
  });
  assert.deepEqual(stderr, ['adapter diagnostic\n']);
  assert.ok(logs.some((entry) => entry.level === 'stderr'));
  assert.equal(child.killCalls, 0);
});

test('process exit waits for a final stdout frame to drain', async () => {
  class FakeChild extends EventEmitter {
    constructor() {
      super();
      this.stdin = new PassThrough();
      this.stdout = new PassThrough();
      this.stderr = new PassThrough();
      this.exitCode = null;
      this.signalCode = null;
    }

    kill() {
      throw new Error('an exited process must not be killed');
    }
  }

  const child = new FakeChild();
  const peer = NdjsonRpcPeer.fromChild(child, {
    defaultTimeoutMs: 1_000,
    exitDrainTimeoutMs: 100,
  });
  const output = lineReader(child.stdin);
  const pending = peer.request('last/request');
  const request = await output.next();

  // Force the ordering Node permits: process exit is observed while stdout is
  // still draining, then the last frame and EOF arrive.
  child.exitCode = 0;
  child.emit('exit', 0, null);
  child.stdout.end(`${JSON.stringify({
    jsonrpc: '2.0',
    id: request.id,
    result: 'drained',
  })}\n`);

  assert.equal(await pending, 'drained');
  const closeReason = await peer.closed;
  assert.ok(closeReason instanceof JsonRpcTransportError);
  assert.equal(closeReason.exitCode, 0);
});

test('process exit has a bounded fallback when stdout never drains', async () => {
  class FakeChild extends EventEmitter {
    constructor() {
      super();
      this.stdin = new PassThrough();
      this.stdout = new PassThrough();
      this.stderr = new PassThrough();
      this.exitCode = null;
      this.signalCode = null;
    }

    kill() {
      throw new Error('an exited process must not be killed');
    }
  }

  const child = new FakeChild();
  const peer = NdjsonRpcPeer.fromChild(child, {
    defaultTimeoutMs: 1_000,
    exitDrainTimeoutMs: 20,
  });
  const output = lineReader(child.stdin);
  const pending = peer.request('will/not/finish');
  await output.next();

  child.exitCode = 9;
  child.emit('exit', 9, null);

  await assert.rejects(pending, (error) => {
    assert.ok(error instanceof JsonRpcTransportError);
    assert.equal(error.exitCode, 9);
    assert.match(error.message, /before stdout finished draining/);
    return true;
  });
});

test('dispose is idempotent, rejects pending requests, and kills an owned process once', async () => {
  class FakeChild extends EventEmitter {
    constructor() {
      super();
      this.stdin = new PassThrough();
      this.stdout = new PassThrough();
      this.stderr = new PassThrough();
      this.exitCode = null;
      this.signalCode = null;
      this.killCalls = 0;
    }

    kill(signal) {
      assert.equal(signal, 'SIGTERM');
      this.killCalls += 1;
      return true;
    }
  }

  const child = new FakeChild();
  const peer = NdjsonRpcPeer.fromChild(child, { defaultTimeoutMs: 1_000 });
  const output = lineReader(child.stdin);
  const pending = peer.request('pending');
  await output.next();

  peer.dispose('test shutdown');
  peer.dispose('second shutdown');

  await assert.rejects(pending, /test shutdown/);
  await assert.rejects(peer.notify('later'), /test shutdown/);
  assert.equal(child.killCalls, 1);
  assert.equal(await peer.closed, peer.closeReason);
});

test('spawnPeer launches directly without a shell', async (t) => {
  assert.throws(
    () => spawnPeer(process.execPath, [], { spawnOptions: { shell: true } }),
    /does not permit shell/,
  );
  assert.throws(
    () => spawnPeer(process.execPath, [], { spawnOptions: { stdio: 'inherit' } }),
    /owns stdio/,
  );

  const script = [
    "let input = '';",
    "process.stdin.setEncoding('utf8');",
    "process.stdin.on('data', chunk => {",
    "  input += chunk;",
    "  const newline = input.indexOf('\\n');",
    "  if (newline === -1) return;",
    "  const request = JSON.parse(input.slice(0, newline));",
    "  process.stdout.write(JSON.stringify({jsonrpc:'2.0', id:request.id, result:{method:request.method}}) + '\\n');",
    '});',
  ].join('\n');

  const peer = spawnPeer(process.execPath, ['-e', script], {
    cwd: process.cwd(),
    env: { ...process.env, NDJSON_RPC_TEST: '1' },
    shell: false,
    defaultTimeoutMs: 2_000,
  });
  t.after(() => peer.dispose());

  assert.deepEqual(await peer.request('spawn/check'), { method: 'spawn/check' });
});

test('spawned agent can send its final response and immediately exit', async () => {
  const fixture = path.join(__dirname, 'fixtures', 'fake-agent.js');
  const peer = spawnPeer(process.execPath, [fixture], {
    defaultTimeoutMs: 2_000,
    exitDrainTimeoutMs: 500,
  });

  assert.equal(await peer.request('fake/respond_and_exit'), 'final response');
  const closeReason = await peer.closed;
  assert.ok(closeReason instanceof JsonRpcTransportError);
  assert.equal(closeReason.exitCode, 0);
});

test('AbortSignal rejects a request and sends protocol cancellation', async (t) => {
  const { peer, output } = makePeer();
  t.after(() => peer.dispose());
  const controller = new AbortController();
  const pending = peer.request('cancel/me', {}, { signal: controller.signal });
  const request = await output.next();

  controller.abort();
  await assert.rejects(pending, (error) => error.name === 'AbortError');
  assert.deepEqual(await output.next(), {
    jsonrpc: '2.0',
    method: '$/cancel_request',
    params: { requestId: request.id },
  });
  assert.equal(peer.disposed, false);
});

test('rejects an unterminated final frame when stdout ends', async () => {
  const { peer, readable, output } = makePeer();
  const pending = peer.request('pending');
  await output.next();
  readable.end('{"jsonrpc":"2.0","id":1,"result":true}');

  await assert.rejects(pending, (error) => {
    assert.ok(error instanceof JsonRpcProtocolError);
    assert.match(error.message, /unterminated/);
    return true;
  });
});
