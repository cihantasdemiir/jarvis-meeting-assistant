"""LLM testleri için taklitler. Gerçek Ollama/model kullanılmaz; sonuçlar model performansı göstermez."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jarvis.llm.client import ChatResult, ChatStats, LlmTimeout


class FakeLlm:
    """chat_json çağrılarını `responder(system, user_payload, schema)` ile yanıtlar.

    responder bir dict döndürürse model JSON'u sayılır; Exception döndürürse/fırlatırsa o hata verilir.
    """

    def __init__(self, responder: Callable[[str, dict, dict], Any] | Any, models: list[str] | None = None):
        self.responder = responder if callable(responder) else (lambda s, u, sc: responder)
        self.calls: list[dict] = []
        self.models = models if models is not None else ["qwen3:4b", "qwen3.5:4b"]

    def chat_json(self, model, system, user, schema, timeout, num_predict=1024):
        payload = json.loads(user) if user.strip().startswith("{") else {"raw": user}
        self.calls.append({"model": model, "system": system, "user": payload, "schema": schema})
        result = self.responder(system, payload, schema)
        if isinstance(result, Exception):
            raise result
        return ChatResult(data=result, raw=json.dumps(result, ensure_ascii=False),
                          stats=ChatStats(model=model, wall_s=0.01))

    def has_model(self, model, timeout=10.0):
        return model in self.models

    def version(self, timeout=5.0):
        return "test"

    def running_models(self, timeout=5.0):
        from jarvis.llm.client import RunningModel

        return [RunningModel(name=m, size=100, size_vram=100) for m in self.models]


def router_answer(intent: str, **arguments) -> dict:
    return {"intent": intent, "arguments": arguments, "confidence": 0.9}


def timeout_responder(*_args):
    return LlmTimeout("test zaman aşımı")


class FakeOllamaServer:
    """Ollama REST API'sini taklit eden yerel HTTP sunucusu (istemciyi gerçek HTTP ile test etmek için)."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.chat_handler: Callable[[dict], tuple[int, dict]] = lambda body: (200, {
            "message": {"role": "assistant", "content": json.dumps({"ok": True})},
            "total_duration": 2_000_000_000, "load_duration": 500_000_000, "prompt_eval_count": 10,
            "eval_count": 5, "eval_duration": 1_000_000_000})
        self.tags = {"models": [{"name": "qwen3:4b"}, {"name": "qwen3.5:4b"}]}
        self.ps = {"models": [{"name": "qwen3:4b", "size": 4_000, "size_vram": 3_000}]}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # sessiz
                pass

            def _send(self, status: int, body: dict):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                server.requests.append(("GET", self.path, None))
                routes = {"/api/version": {"version": "0.0-test"}, "/api/tags": server.tags, "/api/ps": server.ps}
                if self.path in routes:
                    self._send(200, routes[self.path])
                else:
                    self._send(404, {"error": "bulunamadı"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                server.requests.append(("POST", self.path, body))
                status, response = server.chat_handler(body)
                self._send(status, response)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
