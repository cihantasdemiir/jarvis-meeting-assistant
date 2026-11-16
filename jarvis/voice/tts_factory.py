"""Ayarlara göre sesli yanıt motorunu seçer (Faz 5b).

  * `--no-tts` → hiçbir ses motoru başlatılmaz, model yüklenmez, ses paketleri içe aktarılmaz.
  * TTS_ENGINE=none → yalnızca konsol.
  * TTS_ENGINE=pocket (varsayılan) → Pocket TTS Turkish. Kullanılamazsa uyarı + konsol; İngilizce sese GEÇİLMEZ.
  * TTS_ENGINE=pyttsx3 → eski Windows SAPI5 motoru (yalnızca açıkça seçilirse).
"""
from __future__ import annotations

from typing import Any, Callable, TextIO

from .speech_engine import SpeechEngine

BackendFactory = Callable[[Any], Any]
OutputFactory = Callable[[Any], Any]


def default_backend(settings) -> Any:
    from .pocket_backend import PocketTurkishBackend  # noqa: PLC0415

    return PocketTurkishBackend(model_dir=settings.tts_model_dir, repo=settings.tts_model_repo,
                                device=settings.tts_device, cpu_threads=settings.tts_cpu_threads)


def default_output(settings) -> Any:
    from .audio_output import PyAudioOutput  # noqa: PLC0415

    return PyAudioOutput(device_index=settings.tts_output_device)


def create_speech_engine(settings, enabled: bool, *, blocking: bool = False, console: TextIO | None = None,
                         backend_factory: BackendFactory | None = None, output_factory: OutputFactory | None = None,
                         notify: Callable[[str], None] | None = None):
    if not enabled:
        return SpeechEngine(enabled=False, output=console)
    engine = settings.tts_engine
    if engine == "none":
        speech = SpeechEngine(enabled=False, output=console)
        speech.status_message = "Sesli yanıt kapalı (TTS_ENGINE=none); yanıtlar yalnızca konsola yazılır."
        return speech
    if engine == "pyttsx3":
        return SpeechEngine(enabled=True, rate=settings.tts_rate, volume=settings.tts_volume,
                            voice_hint=settings.tts_voice_hint, output=console)
    from .pocket_speech import PocketSpeechEngine  # noqa: PLC0415

    if notify is not None:
        notify("Türkçe ses modeli yükleniyor (Pocket TTS Turkish, ilk açılışta birkaç saniye sürebilir)...")
    return PocketSpeechEngine(
        (backend_factory or default_backend)(settings), (output_factory or default_output)(settings),
        voice=settings.tts_voice, volume=settings.tts_volume, console=console, max_queue=settings.tts_max_queue,
        prebuffer_ms=settings.tts_prebuffer_ms, max_spoken_chars=settings.tts_max_spoken_chars,
        load_timeout_s=settings.tts_load_timeout_s, blocking=blocking)


def shutdown_speech(speech, timeout: float) -> None:
    """Kapanışta son yanıtın bitmesini bekler; ikinci Ctrl+C beklemeyi keser."""
    shutdown = getattr(speech, "shutdown", None)
    if shutdown is None:
        return
    try:
        shutdown(timeout)
    except KeyboardInterrupt:
        speech.stop()
