"""Ollama istemcisi, gizlilik kuralları ve JSON şema doğrulayıcı (gerçek HTTP, sahte sunucu)."""
from __future__ import annotations

import json
import socket
import time
import urllib.error

import pytest

from jarvis.llm.client import (
    LlmBadOutput,
    LlmPolicyError,
    LlmTimeout,
    LlmUnavailable,
    OllamaClient,
    RunningModel,
    check_privacy,
    extract_json,
    is_local_host,
)
from jarvis.llm.schema import SchemaError, validate, validation_errors
from tests.llm_fakes import FakeOllamaServer

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_chat_sends_structured_request_over_http():
    with FakeOllamaServer() as server:
        client = OllamaClient(server.url, num_ctx=4096, temperature=0.0, think=False, keep_alive="5m")
        result = client.chat_json("qwen3:4b", "sistem", "kullanıcı", SCHEMA, timeout=5)
        method, path, body = server.requests[-1]
    assert (method, path) == ("POST", "/api/chat")
    assert body["format"] == SCHEMA and body["stream"] is False and body["think"] is False
    assert body["options"]["temperature"] == 0.0 and body["options"]["num_ctx"] == 4096
    assert body["messages"][0] == {"role": "system", "content": "sistem"}
    assert result.data == {"ok": True}
    assert result.stats.load_s == pytest.approx(0.5) and result.stats.tokens_per_s == pytest.approx(5.0)


def test_list_models_and_gpu_share():
    with FakeOllamaServer() as server:
        client = OllamaClient(server.url)
        assert client.version() == "0.0-test"
        assert client.has_model("qwen3:4b") and not client.has_model("llama3:8b")
        running = client.running_models()
    assert running[0].processor == "%25 CPU / %75 GPU"
    assert RunningModel("x", 100, 100).processor == "%100 GPU"
    assert RunningModel("x", 100, 0).processor == "%100 CPU"


def test_missing_model_is_unavailable():
    with FakeOllamaServer() as server:
        server.chat_handler = lambda body: (404, {"error": "model 'qwen9' not found"})
        with pytest.raises(LlmUnavailable, match="ollama pull"):
            OllamaClient(server.url).chat_json("qwen9", "s", "u", SCHEMA, timeout=5)


def test_think_field_retry_for_models_without_thinking():
    with FakeOllamaServer() as server:
        def handler(body):
            if "think" in body:
                return 400, {"error": "\"x\" does not support thinking"}
            return 200, {"message": {"content": "{\"ok\": true}"}}
        server.chat_handler = handler
        client = OllamaClient(server.url)
        assert client.chat_json("x", "s", "u", SCHEMA, timeout=5).data == {"ok": True}
        assert client.chat_json("x", "s", "u", SCHEMA, timeout=5).data == {"ok": True}
        posts = [r for r in server.requests if r[0] == "POST"]
    assert len(posts) == 3  # ilk çağrıda bir yeniden deneme, ikincide doğrudan "think" olmadan


def test_timeout_is_reported():
    with FakeOllamaServer() as server:
        def slow(body):
            time.sleep(1.5)
            return 200, {"message": {"content": "{}"}}
        server.chat_handler = slow
        with pytest.raises(LlmTimeout):
            OllamaClient(server.url).chat_json("qwen3:4b", "s", "u", SCHEMA, timeout=0.5)


class RaisingOpener:
    """Gerçek ağ kullanmadan urllib hatası üretir (platformdan bağımsız, deterministik).

    Not: Gerçek kapalı porta bağlanmak Windows'ta zaman aşımı, Linux'ta "connection refused" verir;
    bu yüzden testlerde gerçek port kullanılmaz.
    """

    def __init__(self, error: BaseException):
        self.error = error
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize("error", [
    urllib.error.URLError(ConnectionRefusedError(10061, "bağlantı reddedildi")),  # urlopen'ın sarması
    ConnectionRefusedError(111, "Connection refused"),
    ConnectionResetError(104, "Connection reset by peer"),
    urllib.error.URLError(OSError("Name or service not known")),
])
def test_connection_refused_is_unavailable(error):
    client = OllamaClient("http://127.0.0.1:11434", opener=RaisingOpener(error))
    with pytest.raises(LlmUnavailable, match="ulaşılamadı") as info:
        client.version(timeout=2)
    assert not isinstance(info.value, LlmTimeout)


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"),
    socket.timeout("timed out"),
    urllib.error.URLError(TimeoutError("timed out")),  # Windows'ta kapalı porta bağlanırken görülen durum
    urllib.error.URLError(socket.timeout("timed out")),
])
def test_timeout_is_classified_as_timeout(error):
    client = OllamaClient("http://127.0.0.1:11434", opener=RaisingOpener(error))
    with pytest.raises(LlmTimeout, match="yanıt vermedi"):
        client.version(timeout=2)


def test_router_falls_back_on_both_failure_kinds():
    """Her iki hata türünde de LlmError yakalanır; kurallı sistem çalışmaya devam eder."""
    from jarvis.llm.router import LlmCommandRouter
    from jarvis.voice.command_parser import CommandParser

    for error in (ConnectionRefusedError(111, "refused"), TimeoutError("timed out")):
        client = OllamaClient("http://127.0.0.1:11434", opener=RaisingOpener(error))
        decision = LlmCommandRouter(client, "qwen3:4b", CommandParser()).route("gündemde neler var", [])
        assert decision.kind == "error" and decision.intent is None


def test_invalid_json_from_model():
    with FakeOllamaServer() as server:
        server.chat_handler = lambda body: (200, {"message": {"content": "üzgünüm, yardımcı olamam"}})
        with pytest.raises(LlmBadOutput):
            OllamaClient(server.url).chat_json("qwen3:4b", "s", "u", SCHEMA, timeout=5)


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('<think>düşünce</think>\n{"a": 2}', {"a": 2}),
    ('```json\n{"a": 3}\n```', {"a": 3}),
    ('Cevap: {"a": 4} tamam', {"a": 4}),
])
def test_extract_json_variants(text, expected):
    assert extract_json(text) == expected


# ---------------------------------------------------------------- gizlilik (KVKK)
@pytest.mark.parametrize("host,local", [
    ("http://127.0.0.1:11434", True), ("http://localhost:11434", True), ("http://[::1]:11434", True),
    ("http://192.168.1.20:11434", False), ("https://ollama.example.com", False), ("10.0.0.5:11434", False),
])
def test_local_host_detection(host, local):
    assert is_local_host(host) is local


def test_remote_host_refused_unless_allowed():
    with pytest.raises(LlmPolicyError, match="LLM_ALLOW_REMOTE"):
        OllamaClient("http://192.168.1.20:11434")
    OllamaClient("http://192.168.1.20:11434", allow_remote=True)  # açık izinle kabul


def test_cloud_model_refused_before_sending():
    with FakeOllamaServer() as server:
        client = OllamaClient(server.url)
        with pytest.raises(LlmPolicyError, match="bulut"):
            client.chat_json("gpt-oss:120b-cloud", "s", "u", SCHEMA, timeout=5)
        assert not [r for r in server.requests if r[0] == "POST"]  # istek hiç gönderilmedi
    check_privacy("http://127.0.0.1:11434", "qwen3:4b", allow_remote=False)


# ---------------------------------------------------------------- şema doğrulayıcı
ROUTER_LIKE = {
    "type": "object", "additionalProperties": False, "required": ["intent", "n"],
    "properties": {"intent": {"type": "string", "enum": ["A", "B"]},
                   "n": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                   "items": {"type": "array", "maxItems": 2, "items": {"type": "string", "pattern": "^L[0-9]+$"}}},
}


def test_schema_accepts_valid():
    validate({"intent": "A", "n": None, "items": ["L1"]}, ROUTER_LIKE)


@pytest.mark.parametrize("value,fragment", [
    ({"intent": "DELETE_ALL", "n": 1}, "izin verilmeyen"),
    ({"intent": "A"}, "zorunlu"),
    ({"intent": "A", "n": 1, "sql": "DROP TABLE"}, "tanımsız alan"),
    ({"intent": "A", "n": 9}, "en fazla"),
    ({"intent": "A", "n": True}, "tür"),
    ({"intent": "A", "n": 1, "items": ["L1", "L2", "L3"]}, "en fazla 2"),
    ({"intent": "A", "n": 1, "items": ["satır 1"]}, "biçim"),
    ([], "tür"),
])
def test_schema_rejects_invalid(value, fragment):
    errors = validation_errors(value, ROUTER_LIKE)
    assert errors and any(fragment in e for e in errors), errors
    with pytest.raises(SchemaError):
        validate(value, ROUTER_LIKE)


def test_router_schema_is_valid_json():
    from jarvis.llm.analyzer import EXTRACT_SCHEMA
    from jarvis.llm.router import ROUTER_SCHEMA

    for schema in (ROUTER_SCHEMA, EXTRACT_SCHEMA):
        json.dumps(schema)
    assert "NONE" in ROUTER_SCHEMA["properties"]["intent"]["enum"]
    assert "CONFIRM" not in ROUTER_SCHEMA["properties"]["intent"]["enum"]  # onay LLM'den gelemez
