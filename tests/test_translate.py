"""Wire-format translation between OpenAI's APIs and the Codex backend.

These are the rules the backend enforces by rejecting requests, so each test
pins a constraint that was learned the hard way.
"""
from __future__ import annotations

import json

import pytest

from codex_gateway import translate


class TestCoerceBody:
    def test_forces_stream_and_store(self):
        # "Stream must be set to true" / "Store must be set to false"
        out = translate.coerce_body({"model": "m", "stream": False, "store": True})
        assert out["stream"] is True
        assert out["store"] is False

    def test_bare_string_input_becomes_a_list(self):
        # "Input must be a list"
        assert translate.coerce_body({"input": "hello"})["input"] == [
            {"role": "user", "content": "hello"}
        ]

    def test_list_input_is_left_alone(self):
        items = [{"role": "user", "content": "hi"}]
        assert translate.coerce_body({"input": items})["input"] == items

    def test_does_not_mutate_the_caller_body(self):
        body = {"model": "m", "stream": False}
        translate.coerce_body(body)
        assert body["stream"] is False

    def test_structured_system_message_becomes_developer(self):
        # "System messages are not allowed". This is the exact shape VS Code
        # Chat's custom-endpoint provider sends, and the one the backend
        # rejects; the loose string form below it is coerced upstream instead.
        item = {"type": "message", "role": "system",
                "content": [{"type": "input_text", "text": "Be terse."}]}
        assert translate.coerce_body({"input": [item]})["input"] == [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "Be terse."}]}
        ]

    def test_loose_system_message_becomes_developer(self):
        assert translate.coerce_body(
            {"input": [{"role": "system", "content": "Be terse."}]}
        )["input"] == [{"role": "developer", "content": "Be terse."}]

    def test_other_roles_and_non_message_items_are_untouched(self):
        items = [
            {"role": "user", "content": "hi"},
            {"role": "developer", "content": "already fine"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
            "not a dict",
        ]
        assert translate.coerce_body({"input": list(items)})["input"] == items

    def test_does_not_mutate_a_caller_input_item(self):
        item = {"role": "system", "content": "Be terse."}
        translate.coerce_body({"input": [item]})
        assert item["role"] == "system"

    @pytest.mark.parametrize("field", translate.UNSUPPORTED_PARAMETERS)
    def test_rejected_tuning_parameters_are_dropped(self, field):
        # "Unsupported parameter: temperature" — these fail the whole request
        # rather than being ignored. VS Code Chat sends temperature every time.
        assert field not in translate.coerce_body({"model": "m", field: 1})

    def test_reasoning_survives(self):
        # The one tuning control the backend does accept, and the one that
        # makes the model picker's Thinking Effort setting mean anything.
        out = translate.coerce_body({"model": "m", "reasoning": {"effort": "low"}})
        assert out["reasoning"] == {"effort": "low"}


class TestCodexHeaders:
    def test_pins_the_first_party_originator(self):
        # The backend gates part of its surface on a recognised originator.
        headers = translate.codex_headers("tok", "acct-1")
        assert headers["originator"] == "codex_cli_rs"
        assert headers["User-Agent"].startswith("codex_cli_rs/")
        assert headers["Authorization"] == "Bearer tok"
        assert headers["ChatGPT-Account-ID"] == "acct-1"

    def test_account_header_omitted_when_unknown(self):
        assert "ChatGPT-Account-ID" not in translate.codex_headers("tok", None)


class TestFlattenContent:
    def test_plain_string(self):
        assert translate.flatten_content("hi") == "hi"

    def test_joins_text_parts(self):
        parts = [{"type": "text", "text": "a"}, {"type": "input_text", "text": "b"}]
        assert translate.flatten_content(parts) == "ab"

    def test_drops_non_text_parts(self):
        parts = [{"type": "text", "text": "a"},
                 {"type": "image_url", "image_url": {"url": "http://x"}}]
        assert translate.flatten_content(parts) == "a"

    def test_none_becomes_empty(self):
        assert translate.flatten_content(None) == ""


class TestMessagesToInput:
    def test_system_becomes_developer(self):
        out = translate.messages_to_input([{"role": "system", "content": "be terse"}])
        assert out == [{"role": "developer", "content": "be terse"}]

    def test_user_and_assistant_pass_through(self):
        out = translate.messages_to_input([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        assert out == [{"role": "user", "content": "hi"},
                       {"role": "assistant", "content": "hello"}]

    def test_tool_result_becomes_function_call_output(self):
        # Responses has no "tool" role — a result is a standalone item.
        out = translate.messages_to_input([
            {"role": "tool", "tool_call_id": "call-1", "content": "42"},
        ])
        assert out == [{"type": "function_call_output", "call_id": "call-1", "output": "42"}]

    def test_assistant_tool_calls_become_function_call_items(self):
        out = translate.messages_to_input([{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": "echo_shout",
                                         "arguments": '{"text": "hi"}'}}],
        }])
        assert out == [{"type": "function_call", "call_id": "call-1",
                        "name": "echo_shout", "arguments": '{"text": "hi"}'}]

    def test_assistant_text_alongside_tool_calls_is_kept(self):
        out = translate.messages_to_input([{
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        }])
        assert out[0] == {"role": "assistant", "content": "let me check"}
        assert out[1]["type"] == "function_call"

    def test_full_agent_round_trip_survives(self):
        """Bifrost re-sends the whole conversation after running a tool.

        If the call and its result do not survive translation, the follow-up
        request loses the tool output and the model answers with nothing.
        """
        out = translate.messages_to_input([
            {"role": "system", "content": "you have tools"},
            {"role": "user", "content": "shout hello"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-1",
                             "function": {"name": "echo_shout",
                                          "arguments": '{"text": "hello"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "HELLO"},
        ])
        assert [item.get("type") or item.get("role") for item in out] == [
            "developer", "user", "function_call", "function_call_output",
        ]
        assert out[3]["output"] == "HELLO"

    def test_missing_tool_call_id_does_not_crash(self):
        out = translate.messages_to_input([{"role": "tool", "content": "x"}])
        assert out[0]["call_id"] == ""

    def test_arguments_default_to_empty_object(self):
        out = translate.messages_to_input([{
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "f"}}],
        }])
        assert out[0]["arguments"] == "{}"

    @pytest.mark.parametrize("messages", [None, [], [None], ["not a dict"]])
    def test_degenerate_inputs(self, messages):
        assert translate.messages_to_input(messages) == []

    def test_defaults_missing_role_to_user(self):
        assert translate.messages_to_input([{"content": "hi"}]) == [
            {"role": "user", "content": "hi"}
        ]


class TestToolsToResponses:
    def test_nested_schema_is_flattened(self):
        # Without this: "Missing required parameter: 'tools[0].name'"
        out = translate.tools_to_responses([{
            "type": "function",
            "function": {"name": "echo", "description": "d",
                         "parameters": {"type": "object"}, "strict": True},
        }])
        assert out == [{"type": "function", "name": "echo", "description": "d",
                        "parameters": {"type": "object"}, "strict": True}]

    def test_optional_fields_are_omitted_when_absent(self):
        out = translate.tools_to_responses([{"type": "function",
                                             "function": {"name": "echo"}}])
        assert out == [{"type": "function", "name": "echo"}]

    def test_strict_false_is_preserved(self):
        out = translate.tools_to_responses([{
            "type": "function", "function": {"name": "e", "strict": False},
        }])
        assert out[0]["strict"] is False

    def test_already_flat_tool_passes_through(self):
        flat = {"type": "function", "name": "echo"}
        assert translate.tools_to_responses([flat]) == [flat]

    def test_builtin_tool_type_passes_through(self):
        builtin = {"type": "web_search"}
        assert translate.tools_to_responses([builtin]) == [builtin]

    @pytest.mark.parametrize("tools", [None, [], ["nonsense"]])
    def test_degenerate_inputs(self, tools):
        assert translate.tools_to_responses(tools) == []


class TestChatRequestToResponses:
    def test_builds_a_valid_upstream_request(self):
        out = translate.chat_request_to_responses({
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "low"},
            "stream": False,
        })
        assert out["model"] == "gpt-5.6-sol"
        assert out["input"] == [{"role": "user", "content": "hi"}]
        assert out["reasoning"] == {"effort": "low"}
        # Invariants apply regardless of what the client asked for.
        assert out["stream"] is True
        assert out["store"] is False

    def test_unknown_chat_parameters_are_dropped(self):
        # The Responses API rejects unknown chat-completions parameters.
        out = translate.chat_request_to_responses({
            "model": "m", "messages": [], "frequency_penalty": 1, "n": 3, "user": "u",
        })
        assert "frequency_penalty" not in out
        assert "n" not in out
        assert "user" not in out

    @pytest.mark.parametrize("field", translate.PASSTHROUGH_FIELDS)
    def test_passthrough_fields(self, field):
        out = translate.chat_request_to_responses({"model": "m", "messages": [],
                                                   field: "value"})
        assert out[field] == "value"

    def test_tools_and_tool_choice(self):
        out = translate.chat_request_to_responses({
            "model": "m", "messages": [],
            "tools": [{"type": "function", "function": {"name": "e"}}],
            "tool_choice": "auto",
        })
        assert out["tools"] == [{"type": "function", "name": "e"}]
        assert out["tool_choice"] == "auto"

    def test_absent_tools_are_not_sent(self):
        out = translate.chat_request_to_responses({"model": "m", "messages": []})
        assert "tools" not in out
        assert "tool_choice" not in out


class TestDecodeSseLine:
    def test_data_event(self):
        kind, event = translate.decode_sse_line(b'data: {"type": "x"}')
        assert (kind, event) == (translate.SSE_EVENT, {"type": "x"})

    def test_done_sentinel(self):
        assert translate.decode_sse_line(b"data: [DONE]")[0] == translate.SSE_DONE

    @pytest.mark.parametrize("line", [b"", b"\n", b": keepalive", b"event: ping",
                                      b"data: {broken", b"data: "])
    def test_noise_is_skipped_not_fatal(self, line):
        # A single malformed frame must not kill a live response.
        assert translate.decode_sse_line(line)[0] == translate.SSE_SKIP

    def test_accepts_str_as_well_as_bytes(self):
        assert translate.decode_sse_line('data: {"a": 1}') == (translate.SSE_EVENT, {"a": 1})

    def test_undecodable_bytes_do_not_raise(self):
        assert translate.decode_sse_line(b"data: \xff\xfe")[0] == translate.SSE_SKIP


def text_delta(text):
    return {"type": "response.output_text.delta", "delta": text}


def call_added(item_id, call_id, name):
    return {"type": "response.output_item.added",
            "item": {"type": "function_call", "id": item_id,
                     "call_id": call_id, "name": name}}


def args_delta(item_id, delta):
    return {"type": "response.function_call_arguments.delta",
            "item_id": item_id, "delta": delta}


class TestResponseAggregator:
    def test_collects_text(self):
        agg = translate.ResponseAggregator()
        for event in (text_delta("Hel"), text_delta("lo")):
            agg.feed(event)
        body = agg.to_chat_completion(chunk_id="c", created=1, model="m")
        assert body["choices"][0]["message"]["content"] == "Hello"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["object"] == "chat.completion"

    def test_empty_response_has_null_content(self):
        body = translate.ResponseAggregator().to_chat_completion(
            chunk_id="c", created=1, model="m")
        assert body["choices"][0]["message"]["content"] is None

    def test_collects_tool_calls_from_argument_deltas(self):
        agg = translate.ResponseAggregator()
        agg.feed(call_added("item-1", "call-1", "echo_shout"))
        agg.feed(args_delta("item-1", '{"text":'))
        agg.feed(args_delta("item-1", ' "hi"}'))
        body = agg.to_chat_completion(chunk_id="c", created=1, model="m")

        calls = body["choices"][0]["message"]["tool_calls"]
        assert calls == [{"id": "call-1", "type": "function",
                          "function": {"name": "echo_shout",
                                       "arguments": '{"text": "hi"}'}}]
        assert body["choices"][0]["finish_reason"] == "tool_calls"

    def test_arguments_done_replaces_accumulated_deltas(self):
        # `done` carries the complete string, so appending would duplicate it.
        agg = translate.ResponseAggregator()
        agg.feed(call_added("item-1", "call-1", "f"))
        agg.feed(args_delta("item-1", '{"partial'))
        agg.feed({"type": "response.function_call_arguments.done",
                  "item_id": "item-1", "arguments": '{"text": "hi"}'})
        calls = agg.to_chat_completion(chunk_id="c", created=1,
                                       model="m")["choices"][0]["message"]["tool_calls"]
        assert calls[0]["function"]["arguments"] == '{"text": "hi"}'

    def test_call_id_is_preferred_over_item_id(self):
        # The client must echo back call_id; item_id only keys the stream.
        agg = translate.ResponseAggregator()
        agg.feed(call_added("item-1", "call-XYZ", "f"))
        calls = agg.to_chat_completion(chunk_id="c", created=1,
                                       model="m")["choices"][0]["message"]["tool_calls"]
        assert calls[0]["id"] == "call-XYZ"

    def test_empty_arguments_default_to_empty_object(self):
        agg = translate.ResponseAggregator()
        agg.feed(call_added("item-1", "call-1", "f"))
        calls = agg.to_chat_completion(chunk_id="c", created=1,
                                       model="m")["choices"][0]["message"]["tool_calls"]
        assert calls[0]["function"]["arguments"] == "{}"

    def test_text_and_tool_calls_together(self):
        agg = translate.ResponseAggregator()
        agg.feed(text_delta("checking"))
        agg.feed(call_added("item-1", "call-1", "f"))
        message = agg.to_chat_completion(chunk_id="c", created=1,
                                         model="m")["choices"][0]["message"]
        assert message["content"] == "checking"
        assert len(message["tool_calls"]) == 1

    def test_non_function_items_are_ignored(self):
        agg = translate.ResponseAggregator()
        agg.feed({"type": "response.output_item.added",
                  "item": {"type": "message", "id": "m1"}})
        body = agg.to_chat_completion(chunk_id="c", created=1, model="m")
        assert "tool_calls" not in body["choices"][0]["message"]

    def test_unknown_events_are_ignored(self):
        agg = translate.ResponseAggregator()
        agg.feed({"type": "response.created"})
        agg.feed({"type": "response.completed"})
        assert agg.to_chat_completion(chunk_id="c", created=1,
                                      model="m")["choices"][0]["message"]["content"] is None


class TestChatStreamTranslator:
    def test_text_delta_becomes_content_delta(self):
        assert translate.ChatStreamTranslator().feed(text_delta("hi")) == [{"content": "hi"}]

    def test_tool_call_is_announced_before_arguments(self):
        translator = translate.ChatStreamTranslator()
        announced = translator.feed(call_added("item-1", "call-1", "echo_shout"))
        assert announced == [{"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function",
            "function": {"name": "echo_shout", "arguments": ""},
        }]}]

        streamed = translator.feed(args_delta("item-1", '{"text":'))
        assert streamed == [{"tool_calls": [{
            "index": 0, "function": {"arguments": '{"text":'}},
        ]}]

    def test_parallel_calls_get_stable_distinct_indices(self):
        translator = translate.ChatStreamTranslator()
        translator.feed(call_added("item-1", "call-1", "a"))
        translator.feed(call_added("item-2", "call-2", "b"))
        first = translator.feed(args_delta("item-1", "x"))
        second = translator.feed(args_delta("item-2", "y"))
        assert first[0]["tool_calls"][0]["index"] == 0
        assert second[0]["tool_calls"][0]["index"] == 1

    def test_arguments_for_an_unannounced_call_are_dropped(self):
        # Emitting an index we never announced would corrupt the client's state.
        assert translate.ChatStreamTranslator().feed(args_delta("ghost", "x")) == []

    def test_finish_reason_tracks_whether_a_tool_was_called(self):
        translator = translate.ChatStreamTranslator()
        assert translator.finish_reason == "stop"
        translator.feed(call_added("item-1", "call-1", "f"))
        assert translator.finish_reason == "tool_calls"

    def test_unknown_events_produce_no_output(self):
        assert translate.ChatStreamTranslator().feed({"type": "response.created"}) == []


class TestSseChunk:
    def test_encodes_a_well_formed_frame(self):
        raw = translate.sse_chunk(chunk_id="c1", created=99, model="m",
                                  delta={"content": "hi"})
        assert raw.startswith(b"data: ")
        assert raw.endswith(b"\n\n")

        payload = json.loads(raw[6:].decode())
        assert payload["id"] == "c1"
        assert payload["object"] == "chat.completion.chunk"
        assert payload["created"] == 99
        assert payload["choices"][0]["delta"] == {"content": "hi"}
        assert payload["choices"][0]["finish_reason"] is None

    def test_finish_reason_is_carried(self):
        raw = translate.sse_chunk(chunk_id="c", created=1, model="m",
                                  delta={}, finish_reason="tool_calls")
        assert json.loads(raw[6:])["choices"][0]["finish_reason"] == "tool_calls"
