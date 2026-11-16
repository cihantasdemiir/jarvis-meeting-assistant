"""Konuşma tanıma motorları (G-01).

Ortak arayüz: `transcribe(segment) -> SttResult | None`
  * None          : ses anlaşılamadı (gürültü, mırıltı); hata değildir.
  * SttNetworkError: internet / servis hatası; bölüt kaybolmaz, VoiceListener yeniden dener.
  * SttUnavailable : motor kullanılamıyor (kütüphane eksik, yanlış ayar).

Google Web Speech (tr-TR) SpeechRecognition kütüphanesiyle kullanılır. Bu servis sesi
Google sunucularına gönderir (gizlilik notu: README "Gizlilik"). Çevrimdışı faster-whisper
motoru Faz 5'te aynı arayüzle eklenecektir.
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .audio_capture import AudioSegment

log = logging.getLogger(__name__)


class SttError(RuntimeError):
    pass


class SttNetworkError(SttError):
    """Geçici hata: internet yok, servis yanıt vermiyor. Yeniden denenebilir."""


class SttUnavailable(SttError):
    """Kalıcı hata: kütüphane eksik veya motor yapılandırılmamış."""


@dataclass
class SttResult:
    text: str
    alternatives: list[str] = field(default_factory=list)
    confidence: float | None = None
    engine: str = ""
    latency_s: float = 0.0
    # Tanılama: gönderilen sesin süresi ve örnekleme hızı (yavaşlığın nedenini ayırt etmek için)
    audio_s: float = 0.0
    sent_rate: int = 0
    prepare_s: float = 0.0


class SpeechToText(Protocol):
    name: str

    def transcribe(self, segment: AudioSegment) -> SttResult | None: ...


def parse_google_response(response: Any) -> tuple[list[str], float | None]:
    """recognize_google(show_all=True) yanıtını (alternatifler, güven) olarak çözer.

    Eski SpeechRecognition sürümleri boş yanıtta [] döndürür; yenileri UnknownValueError fırlatır.
    İkisi de desteklenir.
    """
    if not response or not isinstance(response, dict):
        return [], None
    alternatives = response.get("alternative") or []
    texts = [str(a.get("transcript", "")).strip() for a in alternatives if a.get("transcript")]
    confidence = None
    for alternative in alternatives:
        if "confidence" in alternative:
            confidence = float(alternative["confidence"])
            break
    return [t for t in texts if t], confidence


class GoogleSpeechToText:
    name = "google"

    def __init__(self, language: str = "tr-TR", timeout_s: float = 8.0, key: str | None = None,
                 recognizer: Any = None, sr_module: Any = None, resample_hz: int = 16000):
        if sr_module is None:
            try:
                import speech_recognition as sr_module  # noqa: PLC0415
            except ImportError as exc:
                raise SttUnavailable("SpeechRecognition kurulu değil. Kurulum: pip install -r requirements-voice.txt") \
                    from exc
        sr = sr_module
        self._sr = sr
        self.language = language
        self.key = key or None
        self.recognizer = recognizer or sr.Recognizer()
        self.recognizer.operation_timeout = timeout_s
        self.timeout_s = timeout_s
        # 48/44.1 kHz mikrofon sesi Google'a olduğu gibi gönderilirse veri ~3 kat büyür;
        # konuşma tanıma için 16 kHz yeterlidir. Daha düşük hızlı kaynak yükseltilmez.
        self.resample_hz = resample_hz

    def transcribe(self, segment: AudioSegment) -> SttResult | None:
        sr = self._sr
        prepare_started = time.monotonic()
        if self.resample_hz and segment.sample_rate > self.resample_hz:
            segment = segment.resampled(self.resample_hz)
        prepare_s = time.monotonic() - prepare_started
        audio = sr.AudioData(segment.pcm, segment.sample_rate, segment.sample_width)
        started = time.monotonic()
        try:
            response = self.recognizer.recognize_google(audio, key=self.key, language=self.language, show_all=True)
        except sr.UnknownValueError:
            return None
        except sr.RequestError as exc:
            raise SttNetworkError(f"Google konuşma tanıma servisine ulaşılamadı: {exc}") from exc
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise SttNetworkError(f"Konuşma tanıma isteği zaman aşımına uğradı: {exc}") from exc
        latency = time.monotonic() - started
        texts, confidence = parse_google_response(response)
        if not texts:
            return None
        return SttResult(text=texts[0], alternatives=texts, confidence=confidence, engine=self.name,
                         latency_s=latency, audio_s=segment.duration, sent_rate=segment.sample_rate,
                         prepare_s=prepare_s)


def create_stt(engine: str, language: str = "tr-TR", timeout_s: float = 8.0, key: str | None = None,
               resample_hz: int = 16000) -> SpeechToText:
    engine = (engine or "google").lower()
    if engine == "google":
        return GoogleSpeechToText(language=language, timeout_s=timeout_s, key=key, resample_hz=resample_hz)
    if engine == "whisper":
        raise SttUnavailable("faster-whisper motoru Faz 5'te eklenecek. Şimdilik STT_ENGINE=google kullanın.")
    raise SttUnavailable(f"Bilinmeyen STT motoru: {engine}. Geçerli değerler: google")
