'use strict';

const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const { TextDecoder } = require('node:util');

const JSON_RPC_VERSION = '2.0';
const DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_EXIT_DRAIN_TIMEOUT_MS = 1_000;
const INTERNAL_ERROR = -32603;
const METHOD_NOT_FOUND = -32601;

class JsonRpcError extends Error {
  constructor(code, message, data, requestId) {
    super(message);
    this.name = 'JsonRpcError';
    this.code = code;
    this.data = data;
    this.requestId = requestId;
  }
}

class JsonRpcTransportError extends Error {
  constructor(message, options = {}) {
    super(message, options);
    this.name = 'JsonRpcTransportError';
  }
}

class JsonRpcProtocolError extends JsonRpcTransportError {
  constructor(message, options = {}) {
    super(message, options);
    this.name = 'JsonRpcProtocolError';
  }
}

class JsonRpcTimeoutError extends Error {
  constructor(method, id, timeoutMs) {
    super(`JSON-RPC request ${method} (${String(id)}) timed out after ${timeoutMs} ms`);
    this.name = 'JsonRpcTimeoutError';
    this.code = 'ETIMEDOUT';
    this.method = method;
    this.requestId = id;
    this.timeoutMs = timeoutMs;
  }
}

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isJsonRpcId(value) {
  return (
    value === null ||
    typeof value === 'string' ||
    (typeof value === 'number' && Number.isFinite(value))
  );
}

function isErrorObject(value) {
  return (
    isRecord(value) &&
    Number.isInteger(value.code) &&
    typeof value.message === 'string'
  );
}

function asError(reason, fallbackMessage) {
  if (reason instanceof Error) {
    return reason;
  }
  if (reason === undefined) {
    return new JsonRpcTransportError(fallbackMessage);
  }
  return new JsonRpcTransportError(`${fallbackMessage}: ${String(reason)}`);
}

function validatePositiveInteger(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new TypeError(`${name} must be a positive safe integer`);
  }
  return value;
}

function validateTimeout(value, name) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a non-negative safe integer`);
  }
  return value;
}

function abortReason(signal) {
  if (signal && signal.reason instanceof Error) {
    return signal.reason;
  }
  const error = new Error('JSON-RPC request was aborted');
  error.name = 'AbortError';
  error.code = 'ABORT_ERR';
  return error;
}

function normalizeResponseError(errorOrCode, message, data) {
  if (Number.isInteger(errorOrCode) && typeof message === 'string') {
    const normalized = { code: errorOrCode, message };
    if (data !== undefined) {
      normalized.data = data;
    }
    return normalized;
  }

  if (isErrorObject(errorOrCode)) {
    const normalized = {
      code: errorOrCode.code,
      message: errorOrCode.message,
    };
    if (hasOwn(errorOrCode, 'data')) {
      normalized.data = errorOrCode.data;
    }
    return normalized;
  }

  if (errorOrCode instanceof Error) {
    return { code: INTERNAL_ERROR, message: errorOrCode.message || 'Internal error' };
  }

  throw new TypeError('JSON-RPC error requires an integer code and string message');
}

/**
 * A JSON-RPC 2.0 peer carried as one UTF-8 JSON object per line.
 *
 * `readable` is the peer's stdout and `writable` is the peer's stdin. When a
 * child process is supplied, its exit closes the transport and `dispose()`
 * terminates it. Callbacks may also be observed through events with the same
 * names: `notification`, `request`, `stderr`, `log`, and `close`.
 */
class NdjsonRpcPeer extends EventEmitter {
  constructor(options = {}) {
    super();

    const child = options.child || options.process;
    const readable = options.readable || (child && child.stdout);
    const writable = options.writable || (child && child.stdin);
    const stderr = options.stderr || (child && child.stderr);

    if (!readable || typeof readable.on !== 'function') {
      throw new TypeError('readable must be a Node readable stream');
    }
    if (!writable || typeof writable.write !== 'function') {
      throw new TypeError('writable must be a Node writable stream');
    }

    this.process = child || null;
    this.readable = readable;
    this.writable = writable;
    this.stderr = stderr || null;
    this.maxLineBytes = validatePositiveInteger(
      options.maxLineBytes === undefined ? DEFAULT_MAX_LINE_BYTES : options.maxLineBytes,
      'maxLineBytes',
    );
    this.defaultTimeoutMs = validateTimeout(
      options.defaultTimeoutMs ?? options.requestTimeoutMs ?? DEFAULT_TIMEOUT_MS,
      'defaultTimeoutMs',
    );
    this.exitDrainTimeoutMs = validatePositiveInteger(
      options.exitDrainTimeoutMs ?? DEFAULT_EXIT_DRAIN_TIMEOUT_MS,
      'exitDrainTimeoutMs',
    );
    this.killOnDispose = options.killOnDispose === undefined ? Boolean(child) : Boolean(options.killOnDispose);
    this.logTraffic = Boolean(options.logTraffic);

    this.onNotification = options.onNotification || null;
    this.onRequest = options.onRequest || null;
    this.onStderr = options.onStderr || null;
    this.onLog = options.onLog || null;
    this.onClose = options.onClose || null;

    for (const [name, callback] of [
      ['onNotification', this.onNotification],
      ['onRequest', this.onRequest],
      ['onStderr', this.onStderr],
      ['onLog', this.onLog],
      ['onClose', this.onClose],
    ]) {
      if (callback !== null && typeof callback !== 'function') {
        throw new TypeError(`${name} must be a function`);
      }
    }

    this._decoder = new TextDecoder('utf-8', { fatal: true });
    this._lineParts = [];
    this._lineBytes = 0;
    this._nextId = 1;
    this._pending = new Map();
    this._writeQueue = Promise.resolve();
    this._disposed = false;
    this._closeReason = null;
    this._listeners = [];
    this._processExit = null;
    this._exitDrainTimer = null;
    this._stdoutEnded = false;

    this.closed = new Promise((resolve) => {
      this._resolveClosed = resolve;
    });

    this._listen(readable, 'data', (chunk) => this._receiveChunk(chunk));
    this._listen(readable, 'end', () => this._readableEnded());
    this._listen(readable, 'error', (error) => {
      this._fail(new JsonRpcTransportError('JSON-RPC stdout failed', { cause: error }));
    });
    this._listen(writable, 'error', (error) => {
      this._fail(new JsonRpcTransportError('JSON-RPC stdin failed', { cause: error }));
    });

    if (stderr && typeof stderr.on === 'function') {
      this._listen(stderr, 'data', (chunk) => this._receiveStderr(chunk));
      this._listen(stderr, 'error', (error) => {
        this._log('warn', 'JSON-RPC stderr stream failed', { error });
      });
    }

    if (child && typeof child.on === 'function') {
      this._listen(child, 'error', (error) => {
        this._fail(new JsonRpcTransportError('Failed to start JSON-RPC process', { cause: error }), false);
      });
      this._listen(child, 'exit', (code, signal) => {
        this._recordProcessExit(code, signal);
      });
      this._listen(child, 'close', (code, signal) => {
        this._processClosed(code, signal);
      });
    }
  }

  static fromChild(child, options = {}) {
    return new NdjsonRpcPeer({ ...options, child });
  }

  get disposed() {
    return this._disposed;
  }

  get closeReason() {
    return this._closeReason;
  }

  setRequestHandler(handler) {
    if (handler !== null && typeof handler !== 'function') {
      throw new TypeError('request handler must be a function or null');
    }
    this.onRequest = handler;
    return this;
  }

  setNotificationHandler(handler) {
    if (handler !== null && typeof handler !== 'function') {
      throw new TypeError('notification handler must be a function or null');
    }
    this.onNotification = handler;
    return this;
  }

  request(method, params, options = {}) {
    this._assertMethod(method);
    if (!isRecord(options)) {
      return Promise.reject(new TypeError('request options must be an object'));
    }
    if (this._disposed) {
      return Promise.reject(this._closeReason);
    }

    const timeoutMs = validateTimeout(
      options.timeoutMs === undefined ? this.defaultTimeoutMs : options.timeoutMs,
      'timeoutMs',
    );
    const signal = options.signal;
    if (
      signal !== undefined &&
      (!signal ||
        typeof signal.aborted !== 'boolean' ||
        typeof signal.addEventListener !== 'function' ||
        typeof signal.removeEventListener !== 'function')
    ) {
      return Promise.reject(new TypeError('signal must be an AbortSignal'));
    }
    if (signal && signal.aborted) {
      return Promise.reject(abortReason(signal));
    }
    const id = this._allocateId();
    const frame = { jsonrpc: JSON_RPC_VERSION, id, method };
    if (params !== undefined) {
      frame.params = params;
    }

    let serialized;
    try {
      serialized = this._serialize(frame);
    } catch (error) {
      return Promise.reject(error);
    }

    const response = new Promise((resolve, reject) => {
      const pending = {
        id,
        method,
        resolve,
        reject,
        timer: null,
        cleanup: null,
      };
      if (signal) {
        const onAbort = () => {
          if (this._pending.get(id) !== pending) {
            return;
          }
          this._pending.delete(id);
          pending.cleanup();
          reject(abortReason(signal));
          void this.notify('$/cancel_request', { requestId: id }).catch(() => {});
        };
        signal.addEventListener('abort', onAbort, { once: true });
        pending.cleanup = () => {
          signal.removeEventListener('abort', onAbort);
          if (pending.timer) {
            clearTimeout(pending.timer);
          }
        };
      } else {
        pending.cleanup = () => {
          if (pending.timer) {
            clearTimeout(pending.timer);
          }
        };
      }
      if (timeoutMs > 0) {
        pending.timer = setTimeout(() => {
          if (this._pending.get(id) !== pending) {
            return;
          }
          this._pending.delete(id);
          pending.cleanup();
          reject(new JsonRpcTimeoutError(method, id, timeoutMs));
          this._log('warn', 'JSON-RPC request timed out', { id, method, timeoutMs });
        }, timeoutMs);
      }
      this._pending.set(id, pending);
    });

    void this._enqueueSerialized(serialized).catch((error) => this._fail(error));
    return response;
  }

  notify(method, params) {
    try {
      this._assertMethod(method);
      if (this._disposed) {
        return Promise.reject(this._closeReason);
      }
      const frame = { jsonrpc: JSON_RPC_VERSION, method };
      if (params !== undefined) {
        frame.params = params;
      }
      return this._enqueueSerialized(this._serialize(frame));
    } catch (error) {
      return Promise.reject(error);
    }
  }

  notification(method, params) {
    return this.notify(method, params);
  }

  dispose(reason) {
    this._close(asError(reason, 'JSON-RPC peer disposed'), this.killOnDispose);
  }

  close(reason) {
    this.dispose(reason);
  }

  _assertMethod(method) {
    if (typeof method !== 'string' || method.length === 0) {
      throw new TypeError('JSON-RPC method must be a non-empty string');
    }
  }

  _allocateId() {
    for (let attempts = 0; attempts <= this._pending.size; attempts += 1) {
      const id = this._nextId;
      this._nextId = id >= Number.MAX_SAFE_INTEGER ? 1 : id + 1;
      if (!this._pending.has(id)) {
        return id;
      }
    }
    throw new JsonRpcTransportError('No JSON-RPC request identifiers are available');
  }

  _serialize(frame) {
    let serialized;
    try {
      serialized = JSON.stringify(frame);
    } catch (error) {
      throw new TypeError(`JSON-RPC frame is not serializable: ${error.message}`, { cause: error });
    }
    const bytes = Buffer.byteLength(serialized, 'utf8');
    if (bytes > this.maxLineBytes) {
      throw new RangeError(`JSON-RPC frame is ${bytes} bytes; limit is ${this.maxLineBytes}`);
    }
    return serialized;
  }

  _enqueueSerialized(serialized) {
    if (this._disposed) {
      return Promise.reject(this._closeReason);
    }

    const operation = this._writeQueue.then(() => {
      if (this._disposed) {
        throw this._closeReason;
      }
      return new Promise((resolve, reject) => {
        try {
          this.writable.write(`${serialized}\n`, 'utf8', (error) => {
            if (error) {
              reject(new JsonRpcTransportError('Could not write JSON-RPC frame', { cause: error }));
            } else {
              resolve();
            }
          });
        } catch (error) {
          reject(new JsonRpcTransportError('Could not write JSON-RPC frame', { cause: error }));
        }
      });
    });

    this._writeQueue = operation.catch(() => {});
    if (this.logTraffic) {
      void operation.then(() => this._log('debug', 'Sent JSON-RPC frame', { frame: serialized }));
    }
    return operation;
  }

  _receiveChunk(chunk) {
    if (this._disposed) {
      return;
    }

    let bytes;
    try {
      bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    } catch (error) {
      this._fail(new JsonRpcProtocolError('JSON-RPC stdout produced a non-byte chunk', { cause: error }));
      return;
    }

    let start = 0;
    while (start < bytes.length && !this._disposed) {
      const newline = bytes.indexOf(0x0a, start);
      if (newline === -1) {
        this._appendLinePart(bytes.subarray(start));
        return;
      }

      this._appendLinePart(bytes.subarray(start, newline));
      if (this._disposed) {
        return;
      }
      this._finishLine();
      start = newline + 1;
    }
  }

  _appendLinePart(part) {
    if (part.length === 0 || this._disposed) {
      return;
    }
    const nextLength = this._lineBytes + part.length;
    if (nextLength > this.maxLineBytes) {
      this._fail(
        new JsonRpcProtocolError(
          `JSON-RPC line exceeds the ${this.maxLineBytes}-byte limit`,
        ),
      );
      return;
    }
    this._lineParts.push(part);
    this._lineBytes = nextLength;
  }

  _finishLine() {
    let line;
    if (this._lineBytes === 0) {
      line = Buffer.alloc(0);
    } else if (this._lineParts.length === 1) {
      line = this._lineParts[0];
    } else {
      line = Buffer.concat(this._lineParts, this._lineBytes);
    }
    this._lineParts = [];
    this._lineBytes = 0;

    if (line.length > 0 && line[line.length - 1] === 0x0d) {
      line = line.subarray(0, line.length - 1);
    }
    if (line.length === 0) {
      return;
    }

    let text;
    let frame;
    try {
      text = this._decoder.decode(line);
      frame = JSON.parse(text);
    } catch (error) {
      this._fail(new JsonRpcProtocolError('Received malformed JSON-RPC frame', { cause: error }));
      return;
    }

    if (this.logTraffic) {
      this._log('debug', 'Received JSON-RPC frame', { frame: text });
    }
    this._receiveFrame(frame);
  }

  _receiveFrame(frame) {
    if (!isRecord(frame) || frame.jsonrpc !== JSON_RPC_VERSION) {
      this._fail(new JsonRpcProtocolError('Received an invalid JSON-RPC 2.0 envelope'));
      return;
    }

    if (hasOwn(frame, 'method')) {
      if (typeof frame.method !== 'string' || frame.method.length === 0) {
        this._fail(new JsonRpcProtocolError('Received a JSON-RPC call with an invalid method'));
        return;
      }
      if (hasOwn(frame, 'id')) {
        if (!isJsonRpcId(frame.id)) {
          this._fail(new JsonRpcProtocolError('Received a JSON-RPC request with an invalid id'));
          return;
        }
        this._receiveRequest(frame);
      } else {
        this._receiveNotification(frame);
      }
      return;
    }

    this._receiveResponse(frame);
  }

  _receiveResponse(frame) {
    if (!hasOwn(frame, 'id') || !isJsonRpcId(frame.id)) {
      this._fail(new JsonRpcProtocolError('Received a JSON-RPC response with an invalid id'));
      return;
    }
    const hasResult = hasOwn(frame, 'result');
    const hasError = hasOwn(frame, 'error');
    if (hasResult === hasError || (hasError && !isErrorObject(frame.error))) {
      this._fail(new JsonRpcProtocolError('Received a malformed JSON-RPC response'));
      return;
    }

    const pending = this._pending.get(frame.id);
    if (!pending) {
      this._log('warn', 'Received a response for an unknown JSON-RPC request', { id: frame.id });
      return;
    }
    this._pending.delete(frame.id);
    pending.cleanup();

    if (hasResult) {
      pending.resolve(frame.result);
    } else {
      pending.reject(new JsonRpcError(frame.error.code, frame.error.message, frame.error.data, frame.id));
    }
  }

  _receiveNotification(frame) {
    const notification = {
      method: frame.method,
      params: frame.params,
      raw: frame,
    };
    this._emitSafely('notification', notification);
    if (this.onNotification) {
      this._runCallback('notification handler', this.onNotification, notification);
    }
  }

  _receiveRequest(frame) {
    let responded = false;
    const send = (payload) => {
      if (responded) {
        return Promise.reject(new Error(`JSON-RPC request ${String(frame.id)} was already answered`));
      }
      responded = true;
      return this._sendFrame({ jsonrpc: JSON_RPC_VERSION, id: frame.id, ...payload });
    };

    const request = {
      id: frame.id,
      method: frame.method,
      params: frame.params,
      raw: frame,
      get responded() {
        return responded;
      },
      respond: (result) => send({ result: result === undefined ? null : result }),
      error: (errorOrCode, message, data) =>
        send({ error: normalizeResponseError(errorOrCode, message, data) }),
      reject: (errorOrCode, message, data) =>
        send({ error: normalizeResponseError(errorOrCode, message, data) }),
    };

    const listenerCount = this.listenerCount('request');
    let eventError = null;
    try {
      this.emit('request', request);
    } catch (error) {
      eventError = error;
      this._log('error', 'JSON-RPC request event listener failed', { error, method: frame.method });
    }

    if (!this.onRequest) {
      if (eventError && !responded) {
        void request.error(INTERNAL_ERROR, 'Internal error').catch((error) => {
          this._log('error', 'Could not send JSON-RPC error response', { error });
        });
      } else if (listenerCount === 0 && !responded) {
        void request.error(METHOD_NOT_FOUND, `Method not found: ${frame.method}`).catch((error) => {
          this._log('error', 'Could not send JSON-RPC method-not-found response', { error });
        });
      }
      return;
    }

    let result;
    try {
      result = this.onRequest(request);
    } catch (error) {
      void Promise.resolve(this._requestHandlerFailed(request, error)).catch((sendError) => {
        this._log('error', 'Could not send JSON-RPC error response', {
          error: sendError,
          method: frame.method,
        });
      });
      return;
    }

    Promise.resolve(result).then(
      (value) => {
        if (value !== undefined && !request.responded) {
          return request.respond(value);
        }
        return undefined;
      },
      (error) => this._requestHandlerFailed(request, error),
    ).catch((error) => {
      this._log('error', 'Could not finish JSON-RPC request handling', { error, method: frame.method });
    });
  }

  _requestHandlerFailed(request, error) {
    this._log('error', 'JSON-RPC request handler failed', { error, method: request.method });
    if (!request.responded) {
      return request.error(INTERNAL_ERROR, 'Internal error');
    }
    return undefined;
  }

  _sendFrame(frame) {
    try {
      if (this._disposed) {
        return Promise.reject(this._closeReason);
      }
      return this._enqueueSerialized(this._serialize(frame));
    } catch (error) {
      return Promise.reject(error);
    }
  }

  _readableEnded() {
    if (this._disposed) {
      return;
    }
    this._stdoutEnded = true;
    if (this._lineBytes > 0) {
      this._fail(new JsonRpcProtocolError('JSON-RPC stdout ended with an unterminated frame'));
    } else if (!this.process) {
      this._fail(new JsonRpcTransportError('JSON-RPC stdout ended'));
    } else if (this._processExit) {
      this._finalizeProcessExit();
    } else {
      // `end` can race the child's `exit` event. Give the process event a
      // bounded window to arrive so normal exit status is preserved, while
      // still closing a transport whose child merely closed stdout and hung.
      this._startExitDrainTimer();
    }
  }

  _recordProcessExit(code, signal) {
    if (this._disposed) {
      return;
    }
    this._processExit = { code, signal };
    if (this._stdoutEnded) {
      this._finalizeProcessExit();
    } else {
      // Node may emit `exit` before the stdout pipe has delivered its final
      // data. Keep the parser attached until `end`/`close`, with a deadline for
      // descendants that inherited stdout and keep the pipe open forever.
      this._startExitDrainTimer();
    }
  }

  _processClosed(code, signal) {
    if (this._disposed) {
      return;
    }
    if (!this._processExit) {
      this._processExit = { code, signal };
    }
    this._finalizeProcessExit();
  }

  _startExitDrainTimer() {
    if (this._disposed || this._exitDrainTimer) {
      return;
    }
    this._exitDrainTimer = setTimeout(() => {
      this._exitDrainTimer = null;
      if (this._disposed) {
        return;
      }
      if (this._processExit) {
        this._finalizeProcessExit(' before stdout finished draining');
      } else {
        this._fail(
          new JsonRpcTransportError(
            `JSON-RPC stdout ended but the process did not exit within ${this.exitDrainTimeoutMs} ms`,
          ),
        );
      }
    }, this.exitDrainTimeoutMs);
  }

  _finalizeProcessExit(detailSuffix = '') {
    const status = this._processExit || {
      code: this.process ? this.process.exitCode : null,
      signal: this.process ? this.process.signalCode : null,
    };
    const detail = status.signal
      ? `signal ${status.signal}`
      : `code ${String(status.code)}`;
    const error = new JsonRpcTransportError(
      `JSON-RPC process exited with ${detail}${detailSuffix}`,
    );
    error.exitCode = status.code;
    error.signal = status.signal;
    this._fail(error, false);
  }

  _receiveStderr(chunk) {
    const text = Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
    this._emitSafely('stderr', text);
    if (this.onStderr) {
      this._runCallback('stderr handler', this.onStderr, text);
    }
    this._log('stderr', text);
  }

  _runCallback(label, callback, value) {
    let result;
    try {
      result = callback(value);
    } catch (error) {
      this._log('error', `${label} failed`, { error });
      return;
    }
    if (result && typeof result.then === 'function') {
      void Promise.resolve(result).catch((error) => {
        this._log('error', `${label} failed`, { error });
      });
    }
  }

  _emitSafely(event, value) {
    try {
      this.emit(event, value);
    } catch (error) {
      this._log('error', `${event} event listener failed`, { error });
    }
  }

  _log(level, message, details = {}) {
    const entry = { level, message, ...details };
    if (this.onLog) {
      try {
        const suffix = typeof details.frame === 'string' ? `: ${details.frame}` : '';
        this.onLog(`${message}${suffix}`, entry);
      } catch {
        // Diagnostics must never take down the ACP transport.
      }
    }
    try {
      this.emit('log', entry);
    } catch {
      // Diagnostics must never take down the ACP transport.
    }
  }

  _listen(emitter, event, listener) {
    emitter.on(event, listener);
    this._listeners.push({ emitter, event, listener });
  }

  _fail(error, killProcess = this.killOnDispose) {
    this._close(asError(error, 'JSON-RPC transport failed'), killProcess);
  }

  _close(error, killProcess) {
    if (this._disposed) {
      return;
    }
    this._disposed = true;
    this._closeReason = error;

    if (this._exitDrainTimer) {
      clearTimeout(this._exitDrainTimer);
      this._exitDrainTimer = null;
    }

    for (const pending of this._pending.values()) {
      pending.cleanup();
      pending.reject(error);
    }
    this._pending.clear();

    for (const { emitter, event, listener } of this._listeners) {
      emitter.removeListener(event, listener);
    }
    this._listeners = [];

    if (killProcess && this.process && typeof this.process.kill === 'function') {
      const running = this.process.exitCode === null || this.process.exitCode === undefined;
      if (running && this.process.signalCode == null) {
        try {
          this.process.kill('SIGTERM');
        } catch (killError) {
          this._log('warn', 'Could not terminate JSON-RPC process', { error: killError });
        }
      }
    }

    if (killProcess && this.writable && typeof this.writable.end === 'function') {
      try {
        this.writable.once('error', () => {});
        this.writable.end();
      } catch {
        // The process may already have closed stdin.
      }
    }

    if (this.onClose) {
      this._runCallback('close handler', this.onClose, error);
    }
    this._emitSafely('close', error);
    this._resolveClosed(error);
  }
}

/**
 * Spawn a JSON-RPC peer without invoking a command shell.
 *
 * Process options belong under `spawnOptions`; all other options are passed to
 * `NdjsonRpcPeer`. Stdio is always three pipes because stdout is the protocol.
 */
function spawnPeer(command, args = [], options = {}) {
  if (typeof command !== 'string' || command.length === 0) {
    throw new TypeError('command must be a non-empty string');
  }
  if (!Array.isArray(args)) {
    options = args || {};
    args = [];
  }
  if (!args.every((argument) => typeof argument === 'string')) {
    throw new TypeError('args must contain only strings');
  }
  if (!isRecord(options)) {
    throw new TypeError('spawn options must be an object');
  }

  const {
    spawnOptions = {},
    maxLineBytes,
    defaultTimeoutMs,
    requestTimeoutMs,
    exitDrainTimeoutMs,
    killOnDispose,
    logTraffic,
    onNotification,
    onRequest,
    onStderr,
    onLog,
    onClose,
    ...flatSpawnOptions
  } = options;
  if (!isRecord(spawnOptions)) {
    throw new TypeError('spawnOptions must be an object');
  }
  const processOptions = { ...flatSpawnOptions, ...spawnOptions };
  if (processOptions.shell !== undefined && processOptions.shell !== false) {
    throw new TypeError('spawnPeer does not permit shell execution');
  }
  if (processOptions.stdio !== undefined) {
    throw new TypeError('spawnPeer owns stdio; custom stdio is not permitted');
  }

  const child = spawn(command, args, {
    ...processOptions,
    shell: false,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  return NdjsonRpcPeer.fromChild(child, {
    ...(maxLineBytes === undefined ? {} : { maxLineBytes }),
    ...(defaultTimeoutMs === undefined ? {} : { defaultTimeoutMs }),
    ...(requestTimeoutMs === undefined ? {} : { requestTimeoutMs }),
    ...(exitDrainTimeoutMs === undefined ? {} : { exitDrainTimeoutMs }),
    ...(killOnDispose === undefined ? {} : { killOnDispose }),
    ...(logTraffic === undefined ? {} : { logTraffic }),
    ...(onNotification === undefined ? {} : { onNotification }),
    ...(onRequest === undefined ? {} : { onRequest }),
    ...(onStderr === undefined ? {} : { onStderr }),
    ...(onLog === undefined ? {} : { onLog }),
    ...(onClose === undefined ? {} : { onClose }),
  });
}

module.exports = {
  DEFAULT_MAX_LINE_BYTES,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_EXIT_DRAIN_TIMEOUT_MS,
  JsonRpcError,
  JsonRpcProtocolError,
  JsonRpcTimeoutError,
  JsonRpcTransportError,
  NdjsonRpcPeer,
  JsonRpcPeer: NdjsonRpcPeer,
  spawnPeer,
  spawnNdjsonRpcPeer: spawnPeer,
};
