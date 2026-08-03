"""Wire-format translation between OpenAI's APIs and ChatGPT's Codex backend.

Every rule here was established by making the request and reading the
rejection. The Codex backend is NOT an OpenAI-compatible API:

  * ``/responses`` only — there is no ``/chat/completions``
  * ``input`` must be a list             ("Input must be a list")
  * ``stream`` must be true              ("Stream must be set to true")
  * ``store`` must be false              ("Store must be set to false")
  * ``originator: codex_cli_rs`` + a codex_cli_rs User-Agent, or the backend
    serves a restricted surface
  * ``ChatGPT-Account-ID``, decoded from the access token's JWT payload

Model names are not the public ones: ``gpt-5.6-sol`` works, bare ``gpt-5.6`` is
rejected. Check ~/.codex/config.toml for what your CLI actually uses.

Pure functions and small state machines only — no I/O, so the whole protocol is
unit-testable without a network or a live subscription.

So there are two formats, and this module converts between them in both
directions. Worked examples follow.


REQUEST — chat/completions in, Responses out
--------------------------------------------
What a client sends us::

    {"model": "gpt-5.6-sol",
     "stream": false,
     "messages": [{"role": "system", "content": "Be terse."},
                  {"role": "user",   "content": "shout hello"}],
     "tools": [{"type": "function",
                "function": {"name": "shout",
                             "parameters": {"type": "object"}}}]}

What we send upstream (``chat_request_to_responses``)::

    {"model": "gpt-5.6-sol",
     "input": [{"role": "developer", "content": "Be terse."},
               {"role": "user",      "content": "shout hello"}],
     "tools": [{"type": "function",
                "name": "shout",
                "parameters": {"type": "object"}}],
     "stream": true,
     "store": false}

Four changes: ``messages`` -> ``input``; role ``system`` -> ``developer``; the
tool schema is flattened (chat nests it under ``function``, Responses does
not); and ``stream``/``store`` are forced regardless of what was asked.


REQUEST — the tool round-trip, where the formats really diverge
---------------------------------------------------------------
An agent loop that runs a tool re-sends the whole conversation afterwards,
including the call and its result — Bifrost's agent mode does exactly this
after executing an MCP tool. In chat/completions those turns are message
*roles*::

    [{"role": "assistant",
      "tool_calls": [{"id": "call_1",
                      "function": {"name": "shout",
                                   "arguments": "{\\"t\\": \\"hello\\"}"}}]},
     {"role": "tool", "tool_call_id": "call_1", "content": "HELLO"}]

In Responses they are not messages at all — they are standalone items, and the
roles disappear (``messages_to_input``)::

    [{"type": "function_call",        "call_id": "call_1",
      "name": "shout",               "arguments": "{\\"t\\": \\"hello\\"}"},
     {"type": "function_call_output", "call_id": "call_1", "output": "HELLO"}]

Without this the follow-up request loses the tool result and the model answers
with nothing.


This is why the translation exists at all: Bifrost forwards the request type it
was given rather than converting chat_completion into responses, so without
this only Responses-native clients could use the subscription.


RESPONSE — Responses SSE in, chat/completions out
--------------------------------------------------
Upstream always streams, even when the client asked for a single body::

    data: {"type": "response.output_text.delta", "delta": "HE"}
    data: {"type": "response.output_text.delta", "delta": "LLO"}
    data: [DONE]

With ``stream: true`` we re-emit it as chat chunks (``ChatStreamTranslator``)::

    data: {"object": "chat.completion.chunk",
           "choices": [{"delta": {"role": "assistant"}, "finish_reason": null}]}
    data: {... "choices": [{"delta": {"content": "HE"},  "finish_reason": null}]}
    data: {... "choices": [{"delta": {"content": "LLO"}, "finish_reason": null}]}
    data: {... "choices": [{"delta": {}, "finish_reason": "stop"}]}
    data: [DONE]

With ``stream: false`` we buffer the same events into one body
(``ResponseAggregator``)::

    {"object": "chat.completion",
     "choices": [{"message": {"role": "assistant", "content": "HELLO"},
                  "finish_reason": "stop"}]}


RESPONSE — streaming a tool call
---------------------------------
Upstream announces the call first, then streams its arguments::

    data: {"type": "response.output_item.added",
           "item": {"type": "function_call", "id": "item_1",
                    "call_id": "call_1", "name": "shout"}}
    data: {"type": "response.function_call_arguments.delta",
           "item_id": "item_1", "delta": "{\\"t\\":"}

which becomes::

    data: {... "delta": {"tool_calls": [{"index": 0, "id": "call_1",
                                         "type": "function",
                                         "function": {"name": "shout",
                                                      "arguments": ""}}]}}
    data: {... "delta": {"tool_calls": [{"index": 0,
                                         "function": {"arguments": "{\\"t\\":"}}]}}

Two details that bite. ``item_id`` is not ``call_id``: later events key off
``item_id``, but the client must echo back ``call_id``, so ``ToolCallAccumulator``
holds both. And chat/completions identifies a streamed call by positional
``index``, which Responses never sends — so the index is assigned on first
sight and reused for every later fragment of that call.
"""
from __future__ import annotations

import json
import os
from typing import Any

CODEX_CLI_VERSION = os.getenv("CODEX_CLI_VERSION", "0.146.0")

# Sentinels returned by decode_sse_line.
SSE_SKIP = "skip"
SSE_EVENT = "event"
SSE_DONE = "done"


def codex_headers(token: str, account_id: str | None = None) -> dict[str, str]:
    """Headers that make the backend serve the full first-party surface."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        # Pinning originator + a codex_cli_rs UA matters: the backend gates part
        # of its surface on a recognised first-party originator.
        "User-Agent": f"codex_cli_rs/{CODEX_CLI_VERSION}",
        "originator": "codex_cli_rs",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def coerce_body(body: dict[str, Any]) -> dict[str, Any]:
    """Apply the backend's non-negotiable request invariants."""
    out = dict(body)
    out["stream"] = True   # "Stream must be set to true"
    out["store"] = False   # "Store must be set to false"

    # "Input must be a list" — accept a bare string for convenience.
    value = out.get("input")
    if isinstance(value, str):
        out["input"] = [{"role": "user", "content": value}]
    return out


def flatten_content(content: Any) -> str:
    """Collapse multimodal content parts to text.

    Non-text parts (images, audio) are dropped: the Responses translation has
    no place to put them, and silently sending an empty part is worse than
    sending the text that surrounds it.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text", "input_text")
        )
    return "" if content is None else str(content)


def messages_to_input(messages: list | None) -> list:
    """OpenAI chat ``messages`` -> Responses ``input``.

    Plain roles map across (system -> developer, multimodal parts flattened to
    text). Tool traffic does NOT: the Responses API represents a tool call and
    its result as standalone items, not as message roles.

        assistant + tool_calls  ->  {"type": "function_call", ...}
        role: "tool"            ->  {"type": "function_call_output", ...}

    Bifrost's agent mode executes an MCP tool and then re-sends the whole
    conversation including those turns, so without this the follow-up request
    loses the tool result and the model answers with nothing.
    """
    out: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = flatten_content(message.get("content", ""))

        if role == "tool":
            out.append({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id") or "",
                "output": content,
            })
            continue

        if role == "assistant" and message.get("tool_calls"):
            if content:
                out.append({"role": "assistant", "content": content})
            for call in message["tool_calls"] or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                out.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": fn.get("name") or call.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        out.append({"role": "developer" if role == "system" else role,
                    "content": content})
    return out


def tools_to_responses(tools: list | None) -> list:
    """chat/completions tool schema -> Responses tool schema.

    Chat completions nests the definition:   {"type":"function","function":{"name":...}}
    Responses flattens it:                   {"type":"function","name":...}

    Without this the backend rejects the request with
    "Missing required parameter: 'tools[0].name'".
    """
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            flat: dict[str, Any] = {"type": "function", "name": fn.get("name")}
            for key in ("description", "parameters", "strict"):
                if fn.get(key) is not None:
                    flat[key] = fn[key]
            out.append(flat)
        else:
            out.append(tool)  # already flat, or a builtin tool type
    return out


# Passed straight through when present; anything else is dropped, because the
# Responses API rejects unknown chat-completions parameters outright.
PASSTHROUGH_FIELDS = ("temperature", "top_p", "max_output_tokens", "reasoning")


def chat_request_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """Build a full Responses request from a chat/completions request."""
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "input": messages_to_input(body.get("messages")),
    }
    for field in PASSTHROUGH_FIELDS:
        if field in body:
            out[field] = body[field]
    if body.get("tools"):
        out["tools"] = tools_to_responses(body["tools"])
    if body.get("tool_choice") is not None:
        out["tool_choice"] = body["tool_choice"]
    return coerce_body(out)


def decode_sse_line(raw: bytes | str) -> tuple[str, Any]:
    """Classify one SSE line as (SSE_EVENT, obj) / (SSE_DONE, None) / (SSE_SKIP, None).

    Shared by the streaming and non-streaming paths so there is exactly one
    place that knows the frame format. Undecodable frames are skipped rather
    than fatal: a single malformed event should not kill a live response.
    """
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text.startswith("data:"):
        return SSE_SKIP, None
    payload = text[5:].strip()
    if payload == "[DONE]":
        return SSE_DONE, None
    try:
        return SSE_EVENT, json.loads(payload)
    except Exception:  # noqa: BLE001
        return SSE_SKIP, None


def sse_chunk(*, chunk_id: str, created: int, model: str,
              delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
    """Encode one chat.completion.chunk SSE frame."""
    return ("data: " + json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }) + "\n\n").encode()


class ToolCallAccumulator:
    """Tracks function calls across Responses events.

    The backend announces a call (``output_item.added``) before its arguments
    arrive, and identifies later events by ``item_id`` — which is not the
    ``call_id`` the client must echo back. This keeps both, plus a stable index
    per call, which the streaming chat format requires.
    """

    def __init__(self) -> None:
        self._order: dict[str, int] = {}
        self._calls: dict[str, dict[str, Any]] = {}

    def add(self, item: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        """Register an announced call. Returns (index, call) or None if the item
        is not a function call."""
        if item.get("type") != "function_call":
            return None
        item_id = item.get("id") or ""
        index = self._order.setdefault(item_id, len(self._order))
        call = {
            "id": item.get("call_id") or item_id,
            "name": item.get("name"),
            "arguments": "",
        }
        self._calls[item_id] = call
        return index, call

    def index_of(self, item_id: str) -> int | None:
        return self._order.get(item_id)

    def append_arguments(self, item_id: str, delta: str) -> None:
        call = self._calls.get(item_id)
        if call is not None:
            call["arguments"] += delta

    def set_arguments(self, item_id: str, arguments: str) -> None:
        call = self._calls.get(item_id)
        if call is not None:
            call["arguments"] = arguments

    def __bool__(self) -> bool:
        return bool(self._calls)

    def to_chat_tool_calls(self) -> list[dict[str, Any]]:
        return [
            {"id": call["id"], "type": "function",
             "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}}
            for call in self._calls.values()
        ]


class ResponseAggregator:
    """Collapses a Responses SSE stream into one chat.completion body.

    Non-streaming callers still require this: the backend only speaks SSE, so
    "non-streaming" means we consume the stream and buffer it.
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.calls = ToolCallAccumulator()

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "response.output_text.delta":
            self.text.append(event.get("delta", ""))
        elif etype == "response.output_item.added":
            self.calls.add(event.get("item") or {})
        elif etype == "response.function_call_arguments.delta":
            self.calls.append_arguments(event.get("item_id") or "", event.get("delta", ""))
        elif etype == "response.function_call_arguments.done":
            # ``done`` carries the complete argument string, so it replaces
            # rather than extends what the deltas built.
            self.calls.set_arguments(event.get("item_id") or "", event.get("arguments", ""))

    def to_chat_completion(self, *, chunk_id: str, created: int, model: str) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text) or None}
        if self.calls:
            message["tool_calls"] = self.calls.to_chat_tool_calls()
        return {
            "id": chunk_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls" if self.calls else "stop",
                "message": message,
            }],
        }


class ChatStreamTranslator:
    """Turns Responses events into chat.completion.chunk deltas.

    ``feed`` returns the deltas one event produced — usually zero or one, never
    buffered — so the caller can write them straight to the client and keep the
    stream live.
    """

    def __init__(self) -> None:
        self.calls = ToolCallAccumulator()
        self.saw_tool_call = False

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        etype = event.get("type")

        if etype == "response.output_text.delta":
            return [{"content": event.get("delta", "")}]

        if etype == "response.output_item.added":
            added = self.calls.add(event.get("item") or {})
            if added is None:
                return []
            index, call = added
            self.saw_tool_call = True
            # Announce the call up front; arguments stream after.
            return [{"tool_calls": [{
                "index": index, "id": call["id"], "type": "function",
                "function": {"name": call["name"], "arguments": ""},
            }]}]

        if etype == "response.function_call_arguments.delta":
            item_id = event.get("item_id") or ""
            index = self.calls.index_of(item_id)
            if index is None:
                return []
            return [{"tool_calls": [{
                "index": index,
                "function": {"arguments": event.get("delta", "")},
            }]}]

        return []

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.saw_tool_call else "stop"
