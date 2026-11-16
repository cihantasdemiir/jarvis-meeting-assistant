"""Yerel Ollama istemcisi (yalnızca Python standart kütüphanesi; ek bağımlılık yok).

Kullanılan uç noktalar (Ollama REST API):
    POST /api/chat     — format=<JSON Schema> ile yapılandırılmış çıktı, stream=false
    GET  /api/tags     — yüklü modeller
    GET  /api/ps       — bellekteki modeller; size_vram/size oranı GPU'ya yüklenen payı gösterir
    GET  /api/version  — sunucu sürümü

Gizlilik (KVKK): Toplantı metni yalnızca bu bilgisayardaki Ollama sunucusuna gönderilir.
  * OLLAMA_HOST localhost/127.0.0.1/::1 dışındaysa LLM_ALLOW_REMOTE=true olmadan reddedilir.
  * Adı "cloud" içeren modeller (Ollama'nın bulutta çalıştırdığı modeller) aynı kuralla reddedilir.
  * İstek ve yanıt içerikleri günlüğe yazılmaz; yalnızca model, süre ve boyut yazılır.

Hata türleri üst katmanın kurallı sisteme geri düşmesi içindir; hiçbir hata uygulamayı durdurmaz.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class LlmError(RuntimeError):
    """LLM kullanılamadı; çağıran kurallı sisteme geri döner."""


class LlmUnavailable(LlmError):
    """Sunucuya ulaşılamıyor, model yüklü değil veya yapılandırma reddedildi."""


class LlmTimeout(LlmError):
    """Model zamanında yanıt vermedi."""


class LlmBadOutput(LlmError):
    """Model geçerli JSON döndürmedi."""


class LlmPolicyError(LlmUnavailable):
    """Gizlilik kuralı nedeniyle istek gönderilmedi (uzak sunucu / bulut modeli)."""


@dataclass
class ChatStats:
    model: str
    wall_s: float
    total_s: float = 0.0
    load_s: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    eval_s: float = 0.0

    @property
    def tokens_per_s(self) -> float:
        return self.output_tokens / self.eval_s if self.eval_s > 0 else 0.0


@dataclass
class ChatResult:
    data: Any  # çözülmüş JSON (henüz şemaya göre doğrulanmamış)
    raw: str
    stats: ChatStats


@dataclass
class RunningModel:
    name: str
    size: int
    size_vram: int
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def gpu_percent(self) -> float:
        return 100.0 * self.size_vram / self.size if self.size else 0.0

    @property
    def processor(self) -> str:
        """`ollama ps` çıktısındaki PROCESSOR sütununa benzer açıklama."""
        gpu = round(self.gpu_percent)
        if gpu >= 100:
            return "%100 GPU"
        if gpu <= 0:
            return "%100 CPU"
        return f"%{100 - gpu} CPU / %{gpu} GPU"


def is_local_host(host: str) -> bool:
    parsed = urlparse(host if "://" in host else f"http://{host}")
    return (parsed.hostname or "").lower() in _LOCAL_HOSTS


def is_cloud_model(model: str) -> bool:
    return "cloud" in (model or "").lower()


def check_privacy(host: str, model: str | None, allow_remote: bool) -> None:
    """Toplantı verisinin bilgisayar dışına çıkmasına yol açacak yapılandırmayı reddeder."""
    if allow_remote:
        return
    if not is_local_host(host):
        raise LlmPolicyError(
            f"OLLAMA_HOST yerel değil ({host}). Toplantı metni başka bir bilgisayara gönderilmez. "
            "Kurum onayıyla izin vermek için .env içinde LLM_ALLOW_REMOTE=true yapın.")
    if model and is_cloud_model(model):
        raise LlmPolicyError(
            f"'{model}' bir bulut modeli gibi görünüyor; toplantı metni internete gönderilmez. "
            "Yerel bir model seçin (ör. qwen3:4b) veya kurum onayıyla LLM_ALLOW_REMOTE=true yapın.")


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Model metninden JSON'u çıkarır. <think> blokları ve ```json çitleri temizlenir."""
    cleaned = _THINK_BLOCK.sub("", text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LlmBadOutput(f"Model geçerli JSON döndürmedi: {exc}") from exc
    raise LlmBadOutput("Model yanıtında JSON bulunamadı.")


class OllamaClient:
    def __init__(self, host: str = "http://127.0.0.1:11434", allow_remote: bool = False,
                 num_ctx: int = 8192, temperature: float = 0.0, think: bool = False, keep_alive: str = "15m",
                 seed: int = 42, opener: Any = None):
        self.host = host.rstrip("/")
        self.allow_remote = allow_remote
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.think = think
        self.keep_alive = keep_alive
        self.seed = seed
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._think_unsupported: set[str] = set()
        check_privacy(self.host, None, allow_remote)

    @classmethod
    def from_settings(cls, settings) -> "OllamaClient":
        return cls(host=settings.ollama_host, allow_remote=settings.llm_allow_remote, num_ctx=settings.llm_num_ctx,
                   temperature=settings.llm_temperature, think=settings.llm_think,
                   keep_alive=settings.llm_keep_alive)

    # ---------------------------------------------------------------- HTTP
    def _request(self, path: str, payload: dict | None, timeout: float) -> dict:
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            if exc.code == 404:
                raise LlmUnavailable(f"Ollama: bulunamadı ({detail or path}). Model yüklü mü? "
                                     f"'ollama pull MODEL' ile indirin.") from exc
            raise LlmUnavailable(f"Ollama HTTP {exc.code}: {detail or exc.reason}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise LlmTimeout(f"Ollama {timeout:.0f} sn içinde yanıt vermedi.") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise LlmTimeout(f"Ollama {timeout:.0f} sn içinde yanıt vermedi.") from exc
            raise LlmUnavailable(f"Ollama sunucusuna ulaşılamadı ({self.host}): {exc.reason}. "
                                 "Ollama uygulaması çalışıyor mu?") from exc
        except (ConnectionError, OSError) as exc:
            raise LlmUnavailable(f"Ollama sunucusuna ulaşılamadı ({self.host}): {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LlmBadOutput("Ollama sunucusu geçersiz yanıt verdi.") from exc

    # ---------------------------------------------------------------- bilgi
    def version(self, timeout: float = 5.0) -> str:
        return str(self._request("/api/version", None, timeout).get("version", "?"))

    def list_models(self, timeout: float = 10.0) -> list[str]:
        models = self._request("/api/tags", None, timeout).get("models") or []
        return [str(m.get("name") or m.get("model")) for m in models]

    def running_models(self, timeout: float = 5.0) -> list[RunningModel]:
        items = self._request("/api/ps", None, timeout).get("models") or []
        return [RunningModel(name=str(m.get("name") or m.get("model")), size=int(m.get("size") or 0),
                             size_vram=int(m.get("size_vram") or 0), details=dict(m.get("details") or {}))
                for m in items]

    def has_model(self, model: str, timeout: float = 10.0) -> bool:
        names = self.list_models(timeout)
        wanted = model if ":" in model else f"{model}:latest"
        return model in names or wanted in names

    # ---------------------------------------------------------------- sohbet
    def chat_json(self, model: str, system: str, user: str, schema: dict, timeout: float,
                  num_predict: int = 1024) -> ChatResult:
        """Yapılandırılmış (JSON Schema) yanıt ister. Doğrulama çağıranın sorumluluğundadır."""
        check_privacy(self.host, model, self.allow_remote)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx, "seed": self.seed,
                        "num_predict": num_predict},
        }
        if model not in self._think_unsupported:
            payload["think"] = self.think
        started = time.monotonic()
        try:
            response = self._request("/api/chat", payload, timeout)
        except LlmUnavailable as exc:
            # Düşünme ayarını desteklemeyen modeller bu alanı reddedebilir: alan olmadan bir kez daha denenir.
            if "think" in payload and "think" in str(exc).lower():
                self._think_unsupported.add(model)
                payload.pop("think")
                response = self._request("/api/chat", payload, max(1.0, timeout - (time.monotonic() - started)))
            else:
                raise
        wall = time.monotonic() - started
        content = str((response.get("message") or {}).get("content") or "")
        ns = 1e9
        stats = ChatStats(model=model, wall_s=wall, total_s=(response.get("total_duration") or 0) / ns,
                          load_s=(response.get("load_duration") or 0) / ns,
                          prompt_tokens=int(response.get("prompt_eval_count") or 0),
                          output_tokens=int(response.get("eval_count") or 0),
                          eval_s=(response.get("eval_duration") or 0) / ns)
        log.info("LLM %s: %.2f sn (yükleme %.2f sn, %d+%d token)", model, wall, stats.load_s,
                 stats.prompt_tokens, stats.output_tokens)
        return ChatResult(data=extract_json(content), raw=content, stats=stats)
