"""Unit tests for Codex CC-↔-Responses translation (v3.0.15)."""
import json
import pytest

from app.providers.codex_translate import (
    chat_completions_to_responses,
    responses_sse_to_chat_completions_sse,
    collect_responses_stream_into_completion,
)


class TestChatCompletionsToResponses:
    def test_system_message_becomes_instructions(self):
        body = {
            "model": "gpt-5.5",
            "messages": [
                {"role": "system", "content": "You are brief."},
                {"role": "user", "content": "Hi."},
            ],
        }
        out = chat_completions_to_responses(body)
        assert out["instructions"] == "You are brief."
        assert out["model"] == "gpt-5.5"
        assert out["stream"] is True
        assert out["store"] is False
        assert out["input"][0]["role"] == "user"
        assert out["input"][0]["content"][0]["type"] == "input_text"
        assert out["input"][0]["content"][0]["text"] == "Hi."

    def test_multiple_system_messages_concatenate(self):
        body = {
            "model": "gpt-5.5",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "system", "content": "Be polite."},
                {"role": "user", "content": "Hi."},
            ],
        }
        out = chat_completions_to_responses(body)
        assert "Be brief." in out["instructions"]
        assert "Be polite." in out["instructions"]

    def test_assistant_message_uses_output_text(self):
        body = {
            "model": "gpt-5.5",
            "messages": [
                {"role": "user", "content": "Hi."},
                {"role": "assistant", "content": "Hello."},
                {"role": "user", "content": "How are you?"},
            ],
        }
        out = chat_completions_to_responses(body)
        items = out["input"]
        assert len(items) == 3
        assert items[1]["role"] == "assistant"
        assert items[1]["content"][0]["type"] == "output_text"

    def test_max_tokens_dropped(self):
        # Codex backend rejects max_output_tokens / max_tokens; we drop them.
        body = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 50}
        out = chat_completions_to_responses(body)
        assert "max_output_tokens" not in out
        assert "max_tokens" not in out

    def test_temperature_passed_through(self):
        body = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.7}
        out = chat_completions_to_responses(body)
        assert out["temperature"] == 0.7

    def test_stream_always_true_upstream(self):
        body = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}],
                "stream": False}  # caller asked non-stream
        out = chat_completions_to_responses(body)
        assert out["stream"] is True   # we MUST upstream as stream

    def test_list_content_extracts_text_chunks(self):
        body = {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Part 1. "},
                {"type": "text", "text": "Part 2."},
            ]}],
        }
        out = chat_completions_to_responses(body)
        assert out["input"][0]["content"][0]["text"] == "Part 1. Part 2."


class TestResponsesSSEToChatCompletionsSSE:
    @pytest.mark.asyncio
    async def test_basic_stream_translation(self):
        async def gen():
            yield "event: response.created"
            yield 'data: {"type":"response.created","response":{"id":"r_1"}}'
            yield ""
            yield "event: response.output_text.delta"
            yield 'data: {"type":"response.output_text.delta","delta":"Hi"}'
            yield ""
            yield "event: response.output_text.delta"
            yield 'data: {"type":"response.output_text.delta","delta":" there"}'
            yield ""
            yield "event: response.completed"
            yield 'data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}'
            yield ""

        chunks = []
        async for chunk in responses_sse_to_chat_completions_sse(gen(), model="gpt-5.5"):
            chunks.append(chunk)

        # Concatenate the raw bytes to make assertions easier
        all_data = b"".join(chunks).decode()
        # Two delta chunks + one finish chunk + DONE
        assert "data: {" in all_data
        assert all_data.endswith("data: [DONE]\n\n")
        # First delta should set role=assistant; subsequent should not
        first_data = chunks[0].decode().split("data: ", 1)[1]
        first_obj = json.loads(first_data.split("\n\n", 1)[0])
        assert first_obj["choices"][0]["delta"]["role"] == "assistant"
        assert first_obj["choices"][0]["delta"]["content"] == "Hi"
        # Finish chunk has finish_reason='stop' + usage
        finish_obj = json.loads(chunks[-2].decode().split("data: ", 1)[1].split("\n\n", 1)[0])
        assert finish_obj["choices"][0]["finish_reason"] == "stop"
        assert finish_obj["usage"]["prompt_tokens"] == 5

    @pytest.mark.asyncio
    async def test_collect_into_non_stream_completion(self):
        async def gen():
            yield 'data: {"type":"response.output_text.delta","delta":"Hello"}'
            yield 'data: {"type":"response.output_text.delta","delta":", world"}'
            yield 'data: {"type":"response.output_text.delta","delta":"!"}'
            yield 'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":3,"total_tokens":6}}}'

        result = await collect_responses_stream_into_completion(gen(), model="gpt-5.5")
        assert result["object"] == "chat.completion"
        assert result["choices"][0]["message"]["content"] == "Hello, world!"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["usage"]["completion_tokens"] == 3

    @pytest.mark.asyncio
    async def test_error_event_surfaces(self):
        async def gen():
            yield 'data: {"type":"response.output_text.delta","delta":"partial"}'
            yield 'data: {"type":"response.error","error":{"type":"rate_limit","message":"slow down"}}'

        result = await collect_responses_stream_into_completion(gen(), model="gpt-5.5")
        assert result["choices"][0]["finish_reason"] == "error"
        assert result["error"]["type"] == "rate_limit"
        assert "partial" in result["choices"][0]["message"]["content"]
