"""Sesli yanıt (TTS) katmanı (G-02).

Her yanıt önce konsola yazılır; TTS etkinse ayrıca seslendirilir. pyttsx3
kurulamazsa, ses motoru başlatılamazsa veya seslendirme sırasında hata olursa
uygulama durmaz, yalnızca konsol çıktısı ile devam eder.

Windows notu: pyttsx3'ün SAPI5 sürücüsü COM kullanır ve motoru oluşturan iş
parçacığına bağlıdır. Süre uyarıları ve mikrofon bildirimleri başka iş
parçacıklarından geldiği için tüm seslendirme TEK bir TTS iş parçacığında yapılır;
`say()` yalnızca işi kuyruğa koyar (ve isteğe bağlı olarak bitmesini bekler).

Yankı koruması: `is_muted(guard_s)` asistan konuşurken ve konuşma bittikten sonraki
kısa süre boyunca True döner. Mikrofon bu sürede gelen sesi atar; böylece asistanın
hoparlörden çıkan sesi komut olarak algılanmaz (talimat Bölüm 6).
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TextIO

from ..utils.turkish_text import tr_lower

log = logging.getLogger(__name__)


@dataclass
class _Job:
    text: str
    done: threading.Event = field(default_factory=threading.Event)


class SpeechEngine:
    # Susturma kuyruğa iş girdiği anda başlar (gerçek oynatma anı bilinmez); bkz. PocketSpeechEngine
    precise_playback_events = False

    def __init__(self, enabled: bool = True, rate: int = 165, volume: float = 1.0,
                 voice_hint: str = "tolga", output: TextIO | None = None,
                 prefix: str = "JARVIS: ", engine_factory: Callable[[], object] | None = None,
                 max_utterance_s: float = 60.0):
        self.enabled = enabled
        self.rate = rate
        self.volume = volume
        self.voice_hint = voice_hint
        self.output = output or sys.stdout
        self.prefix = prefix
        self.voice_name: str | None = None
        self.status_message = "TTS kapalı (yalnızca konsol)."
        self.max_utterance_s = max_utterance_s
        self.on_speech_end: Callable[[], None] | None = None
        self._engine_factory = engine_factory
        self._engine = None
        self._print_lock = threading.Lock()
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._speaking = threading.Event()
        self._last_speech_end = 0.0
        self._ready = threading.Event()
        self._worker: threading.Thread | None = None
        if enabled:
            self._worker = threading.Thread(target=self._run_worker, daemon=True, name="tts")
            self._worker.start()
            self._ready.wait(15)
            if not self._ready.is_set():
                self.enabled = False
                self.status_message = "TTS başlatılamadı (zaman aşımı); yanıtlar yalnızca konsola yazılacak."

    # ---------------------------------------------------------------- iş parçacığı
    def _create_engine(self):
        if self._engine_factory is not None:
            return self._engine_factory()
        import pyttsx3  # noqa: PLC0415

        return pyttsx3.init()

    def _run_worker(self) -> None:
        try:
            try:  # Windows: bu iş parçacığında COM'u başlat (SAPI5 için gerekli)
                import comtypes  # noqa: PLC0415

                comtypes.CoInitialize()
            except Exception:
                pass
            try:
                engine = self._create_engine()
            except ImportError:
                self._disable("pyttsx3 kurulu değil; yanıtlar yalnızca konsola yazılacak.")
                return
            except Exception as exc:  # SAPI5/espeak başlatma hataları ortama bağlıdır
                self._disable(f"TTS başlatılamadı ({exc}); yanıtlar yalnızca konsola yazılacak.")
                return
            try:
                engine.setProperty("rate", self.rate)
                engine.setProperty("volume", self.volume)
                voice = self._select_turkish_voice(engine.getProperty("voices") or [])
                if voice is not None:
                    engine.setProperty("voice", voice.id)
                    self.voice_name = voice.name
                    self.status_message = f"Türkçe ses: {voice.name}"
                else:
                    self.status_message = ("Türkçe ses bulunamadı; varsayılan ses kullanılacak. Kurulum için "
                                           "docs/WINDOWS_SES_KURULUMU.md 'Türkçe ses' bölümüne bakın.")
                    log.warning(self.status_message)
            except Exception as exc:
                log.warning("TTS ses ayarı yapılamadı: %s", exc)
            self._engine = engine
        finally:
            self._ready.set()
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._speaking.set()
            try:
                self._engine.say(job.text)
                self._engine.runAndWait()
            except Exception as exc:
                log.warning("Seslendirme hatası, konsol ile devam ediliyor: %s", exc)
            finally:
                self._last_speech_end = time.monotonic()
                self._speaking.clear()
                job.done.set()
                if self.on_speech_end:
                    try:
                        self.on_speech_end()
                    except Exception:
                        log.exception("on_speech_end hatası")

    def _disable(self, message: str) -> None:
        self.enabled = False
        self.status_message = message
        log.warning(message)

    def _select_turkish_voice(self, voices: list) -> object | None:
        """Önce ayardaki isim (ör. 'tolga'), sonra herhangi bir Türkçe ses seçilir."""
        def describe(voice) -> str:
            languages = " ".join(
                lang.decode("utf-8", "ignore") if isinstance(lang, bytes) else str(lang)
                for lang in (getattr(voice, "languages", None) or [])
            )
            return tr_lower(f"{getattr(voice, 'name', '')} {getattr(voice, 'id', '')} {languages}")

        hint = tr_lower(self.voice_hint or "")
        if hint:
            for voice in voices:
                if hint in describe(voice):
                    return voice
        for voice in voices:
            text = describe(voice)
            if "turkish" in text or "türkçe" in text or "tr-tr" in text or "tr_tr" in text or "_tr" in text:
                return voice
        return None

    # ---------------------------------------------------------------- konuşma
    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set() or not self._jobs.empty()

    def is_muted(self, guard_s: float = 0.7) -> bool:
        """Asistan konuşuyorsa veya konuşması `guard_s` saniye önce bittiyse True (yankı koruması)."""
        if not self.enabled:
            return False
        if self.is_speaking:
            return True
        return (time.monotonic() - self._last_speech_end) < guard_s

    def say(self, text: str, wait: bool = True) -> None:
        """Metni konsola yazar ve (TTS açıksa) seslendirir. Her iş parçacığından çağrılabilir."""
        if not text:
            return
        with self._print_lock:
            print(f"{self.prefix}{text}", file=self.output, flush=True)
        if not self.enabled or self._worker is None or not self._worker.is_alive():
            return
        job = _Job(text)
        self._jobs.put(job)
        if wait and threading.current_thread() is not self._worker:
            timeout = min(self.max_utterance_s, 10 + len(text) / 8)
            if not job.done.wait(timeout):
                # Bazı SAPI5 kurulumlarında runAndWait takılabilir; program kilitlenmesin.
                self._disable("Sesli yanıt yanıt vermedi; bundan sonra yanıtlar yalnızca konsola yazılacak.")
                log.error("TTS zaman aşımı: %s", text[:60])

    current_text = ""  # yankı denetimi için (bu motorda oynatma anı bilinmez)

    def note(self, text: str) -> None:
        """Yalnızca konsola yazar; seslendirmez."""
        with self._print_lock:
            print(f"{self.prefix}{text}", file=self.output, flush=True)

    def cancel_all(self) -> None:
        """Bekleyen yanıtları atar ve çalanı durdurmayı dener (SAPI5'te en iyi çaba; Pocket TTS'te kesin)."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is None:  # kapanış işareti korunur
                self._jobs.put(None)
                break
            job.done.set()
        if self._engine is not None and self._speaking.is_set():
            try:
                self._engine.stop()
            except Exception:
                log.debug("pyttsx3 durdurulamadı", exc_info=True)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Kuyruktaki seslendirmeler bitene kadar bekler (Faz 5b ortak arayüzü)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.enabled and self.is_speaking:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def shutdown(self, timeout: float = 15.0) -> bool:
        """Kapanış: son yanıtın bitmesini en fazla `timeout` sn bekler, sonra motoru durdurur."""
        finished = self.wait_idle(timeout)
        self.stop()
        return finished

    def stop(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._jobs.put(None)
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass
