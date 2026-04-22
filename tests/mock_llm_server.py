"""
Threaded mock OpenAI-compatible LLM server for integration testing.
No external dependencies — uses Python stdlib http.server.

Usage in fixtures:
    from tests.mock_llm_server import start_mock_server
    srv = start_mock_server(port=9876)
    srv.queue_response(type="text", content="Hello")
    received = srv.get_received()
    srv.stop()
"""
import json
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/received":
            with self.server._lock:
                data = list(self.server._received)
            self._json(200, data)
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "mock-gpt", "object": "model"}]})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path == "/received":
            with self.server._lock:
                self.server._received.clear()
            self._json(200, {"cleared": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)
        try:
            body: dict = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            body = {}

        if self.path == "/control":
            self.server._queue.put(body)
            self._json(200, {"queued": True})
        elif self.path == "/received/clear":
            with self.server._lock:
                self.server._received.clear()
            self._json(200, {"cleared": True})
        elif self.path in ("/v1/chat/completions", "/chat/completions"):
            with self.server._lock:
                self.server._received.append(body)
            self._handle_completion(body)
        else:
            self._json(404, {"error": "not found"})

    def _handle_completion(self, req: dict):
        is_stream = req.get("stream", False)
        try:
            spec: dict = self.server._queue.get_nowait()
        except queue.Empty:
            spec = {"type": "text", "content": "OK"}

        resp_type = spec.get("type", "text")

        # Error response
        if resp_type == "error":
            status = spec.get("status", 500)
            msg = spec.get("message", "mock error")
            body_str = spec.get("body", "")
            payload = body_str or json.dumps({"error": {"message": msg, "type": "mock_error", "code": "mock"}})
            self._raw(status, payload.encode(), "application/json")
            return

        # Tool emulation: model returns <tool_call>...</tool_call> text
        if resp_type == "tool_emulation":
            tool_name = spec.get("tool_name", "test_tool")
            tool_input = spec.get("tool_input", {"query": "test"})
            content = f'<tool_call>{json.dumps({"name": tool_name, "input": tool_input})}</tool_call>'
            spec = {"type": "text", "content": content}
            resp_type = "text"

        # Native tool call response
        if resp_type == "tool_call":
            tool_name = spec.get("tool_name", "read_file")
            tool_input = spec.get("tool_input", {"path": "/etc/hosts"})
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            chunk_id = f"mock-{uuid.uuid4().hex[:8]}"
            if is_stream:
                lines = [
                    {"id": chunk_id, "object": "chat.completion.chunk", "created": _ts(), "model": "mock-gpt",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": None,
                                   "tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                   "function": {"name": tool_name, "arguments": ""}}]},
                                  "finish_reason": None}]},
                    {"id": chunk_id, "object": "chat.completion.chunk", "created": _ts(), "model": "mock-gpt",
                     "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {
                                  "arguments": json.dumps(tool_input)}}]}, "finish_reason": None}]},
                    {"id": chunk_id, "object": "chat.completion.chunk", "created": _ts(), "model": "mock-gpt",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                     "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
                ]
                self._sse(lines)
            else:
                data = {
                    "id": chunk_id, "object": "chat.completion", "created": _ts(), "model": "mock-gpt",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                  "tool_calls": [{"id": call_id, "type": "function",
                                                  "function": {"name": tool_name,
                                                               "arguments": json.dumps(tool_input)}}]},
                                 "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
                self._json(200, data)
            return

        # Plain text response
        content: str = spec.get("content", "OK")
        chunk_id = f"mock-{uuid.uuid4().hex[:8]}"
        tok_count = max(1, len(content.split()))

        if is_stream:
            lines = [
                {"id": chunk_id, "object": "chat.completion.chunk", "created": _ts(), "model": "mock-gpt",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": chunk_id, "object": "chat.completion.chunk", "created": _ts(), "model": "mock-gpt",
                 "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                {"id": chunk_id, "object": "chat.completion.chunk", "created": _ts(), "model": "mock-gpt",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": tok_count, "total_tokens": 10 + tok_count}},
            ]
            self._sse(lines)
        else:
            data = {
                "id": chunk_id, "object": "chat.completion", "created": _ts(), "model": "mock-gpt",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": tok_count, "total_tokens": 10 + tok_count},
            }
            self._json(200, data)

    def _sse(self, events: list[dict]):
        parts = []
        for evt in events:
            parts.append(f"data: {json.dumps(evt)}\n\n".encode())
        parts.append(b"data: [DONE]\n\n")
        body = b"".join(parts)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: Any):
        body = json.dumps(data).encode()
        self._raw(code, body, "application/json")

    def _raw(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress access log


def _ts() -> int:
    return int(time.time())


class MockServer:
    def __init__(self, httpd: HTTPServer, thread: threading.Thread):
        self._httpd = httpd
        self._thread = thread

    def queue_response(self, **kwargs):
        self._httpd._queue.put(kwargs)

    def get_received(self) -> list[dict]:
        with self._httpd._lock:
            return list(self._httpd._received)

    def clear_received(self):
        with self._httpd._lock:
            self._httpd._received.clear()

    def stop(self):
        self._httpd.shutdown()
        self._thread.join(timeout=5)


def start_mock_server(port: int = 9876) -> MockServer:
    httpd = HTTPServer(("0.0.0.0", port), _Handler)
    httpd._queue: queue.Queue = queue.Queue()
    httpd._received: list = []
    httpd._lock = threading.Lock()

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    # Wait until port is accepting connections
    import socket
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)

    return MockServer(httpd, thread)
