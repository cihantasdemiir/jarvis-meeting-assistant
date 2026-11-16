"""Mikrofon tabanlı girdi kaynağı (VoiceListener).

TextListener ile aynı arayüzü uygular: `listen()` bir metin döndürür. Döndürülen
nesne `Utterance` (str alt sınıfı) olduğu için yakalama zamanı ve tanıma süresi de
taşınır; üst katman bunları kullanmak zorunda değildir.

Akış:
    AudioCapture ──bölüt──► segment kuyruğu ──► STT iş parçacığı (TEK, sıralı) ──► sonuç kuyruğu ──► listen()

Kurallar:
  * Bölütler geldiği sırayla tanınır. Komutlar ve konuşmalar sıralarını korur; böylece
    "söz Mehmet'te" komutundan önceki konuşma Mehmet'e yazılmaz.
  * İnternet / servis hatasında bölüt atılmaz: kuyruğun başında bekler, artan aralıklarla
    yeniden denenir, ses dosyası güvenlik için diske yazılır ve kullanıcı bir kez bilgilendirilir.
  * Anlaşılamayan ses sessizce sayılır (gürültü komut sanılmaz).
  * Program kapanırken yarım konuşma ve kuyruktaki bölütler işlenip `drain()` ile teslim edilir.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .audio_capture import AudioCapture, AudioSegment, FrameSource, SegmenterConfig
from .stt import SpeechToText, SttError, SttNetworkError, SttResult

log = logging.getLogger(__name__)


class Utterance(str):
    """Tanınan konuşma metni + üst bilgiler. Normal bir `str` gibi kullanılabilir."""

    captured_at: datetime | None
    ended_at: datetime | None
    confidence: float | None
    stt_latency: float | None
    alternatives: tuple[str, ...]
    recognized_at: float | None  # time.monotonic()
    voice_ended_at: datetime | None  # kullanıcının sustuğu an (gecikme ölçümü için)

    def __new__(cls, text: str, captured_at: datetime | None = None, ended_at: datetime | None = None,
                confidence: float | None = None, stt_latency: float | None = None,
                alternatives: tuple[str, ...] = (), recognized_at: float | None = None,
                voice_ended_at: datetime | None = None):
        obj = super().__new__(cls, text)
        obj.captured_at = captured_at
        obj.ended_at = ended_at
        obj.voice_ended_at = voice_ended_at or ended_at
        obj.confidence = confidence
        obj.stt_latency = stt_latency
        obj.alternatives = tuple(alternatives)
        obj.recognized_at = recognized_at
        return obj


@dataclass
class ListenerStats:
    segments: int = 0
    recognized: int = 0
    unintelligible: int = 0
    network_errors: int = 0
    total_stt_latency: float = 0.0
    slow: int = 0  # slow_warning_s süresini aşan tanıma sayısı
    max_stt_latency: float = 0.0

    @property
    def average_latency(self) -> float:
        return self.total_stt_latency / self.recognized if self.recognized else 0.0


def choose_transcript(result: SttResult, is_addressed: Callable[[str], bool] | None) -> str:
    """Google birden çok alternatif döndürebilir. En olası metin uyandırma kelimesiyle
    başlamıyorsa ama bir alternatif başlıyorsa komut olan alternatif seçilir
    (ör. "carvis gündemi oku" yerine "Jarvis gündemi oku")."""
    if is_addressed is None or is_addressed(result.text):
        return result.text
    for alternative in result.alternatives[1:]:
        if is_addressed(alternative):
            return alternative
    return result.text


class VoiceListener:
    def __init__(self, source: FrameSource, stt: SpeechToText, notify: Callable[[str], None] | None = None,
                 is_muted: Callable[[], bool] | None = None, is_addressed: Callable[[str], bool] | None = None,
                 segmenter_config: SegmenterConfig | None = None, pending_dir: Path | None = None,
                 max_retry_s: float = 30.0, initial_retry_s: float = 2.0, slow_warning_s: float = 4.0,
                 keep_partial_on_mute: bool = False, barge_in=None):
        self.stt = stt
        self.slow_warning_s = slow_warning_s
        self.notify = notify or (lambda message: None)
        self.is_addressed = is_addressed
        self.pending_dir = pending_dir
        self.max_retry_s = max_retry_s
        self.initial_retry_s = initial_retry_s
        self.stats = ListenerStats()
        self._segments: queue.Queue[AudioSegment | None] = queue.Queue()
        self._results: queue.Queue[Utterance] = queue.Queue()
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._busy = threading.Event()  # STT iş parçacığı şu an bir bölüt işliyor
        self._offline = False
        self.barge_in = barge_in
        self.capture = AudioCapture(source, on_segment=self._segments.put, on_status=self.notify,
                                    is_muted=is_muted, config=segmenter_config,
                                    keep_partial_on_mute=keep_partial_on_mute, barge_in=barge_in)
        self._worker = threading.Thread(target=self._run_worker, daemon=True, name="stt-worker")
        self._started = False

    # ---------------------------------------------------------------- yaşam döngüsü
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.barge_in is not None:
            self.barge_in.start()
        self.capture.start()
        self._worker.start()

    @property
    def device_name(self) -> str:
        return self.capture.source.name

    def listen(self) -> Utterance | None:
        """Bir sonraki tanınan konuşmayı bekler. Ctrl+C'nin Windows'ta çalışması için kısa aralıklarla bekler."""
        self.start()
        while True:
            try:
                return self._results.get(timeout=0.25)
            except queue.Empty:
                if self._closed.is_set():
                    return None
                if self._source_exhausted():
                    return None

    def _source_exhausted(self) -> bool:
        """Ses kaynağı bitti (ör. WAV dosyası) ve işlenecek hiçbir şey kalmadı mı?"""
        return (self.capture.ended.is_set() and self._segments.empty() and not self._busy.is_set()
                and self._results.empty())

    def close(self, drain_timeout: float = 10.0) -> None:
        """Yakalamayı durdurur; yarım bölüt ve kuyruktakiler mümkünse tanınır."""
        if self.barge_in is not None:
            self.barge_in.close()
        if not self._started or self._closed.is_set():
            self._closed.set()
            return
        self.capture.stop()
        self._segments.put(None)  # bitiş işareti: kuyruktakiler işlendikten sonra durur
        self._worker.join(drain_timeout)
        if self._worker.is_alive():
            log.warning("Kapanışta %d bölüt tanınamadı (süre doldu); ses dosyaları saklandı.",
                        self._segments.qsize())
            self._stop.set()
        self._closed.set()

    def drain(self) -> list[Utterance]:
        """Kapanıştan sonra teslim edilmemiş konuşmaları döndürür."""
        items = []
        while True:
            try:
                items.append(self._results.get_nowait())
            except queue.Empty:
                return items

    # ---------------------------------------------------------------- STT iş parçacığı
    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                segment = self._segments.get(timeout=0.25)
            except queue.Empty:
                continue
            if segment is None:
                return
            self._busy.set()
            try:
                self._process(segment)
            finally:
                self._busy.clear()

    def _process(self, segment: AudioSegment) -> None:
        """Bir bölütü tanır. Ağ hatasında bölüt bırakılmaz: kuyruğun başında beklenir ve yeniden denenir."""
        self.stats.segments += 1
        saved: Path | None = None
        retry = self.initial_retry_s
        while not self._stop.is_set():
            try:
                result = self.stt.transcribe(segment)
            except SttNetworkError as exc:
                self.stats.network_errors += 1
                if not self._offline:
                    self._offline = True
                    self.notify("İnternet bağlantısı yok veya konuşma tanıma servisine ulaşılamıyor. "
                                "Konuşmalar sırayla bekletiliyor; bağlantı gelince işlenecek.")
                    log.warning("STT ağ hatası: %s", exc)
                if saved is None:
                    saved = self._save_pending(segment)
                self._stop.wait(retry)
                retry = min(self.max_retry_s, retry * 2)
                continue
            except SttError as exc:
                log.error("STT hatası, bölüt atlandı: %s", exc)
                self.notify(f"Konuşma tanıma hatası: {exc}")
                result = None
            except Exception:
                log.exception("STT beklenmeyen hata, bölüt atlandı")
                result = None
            if self._offline:
                self._offline = False
                self.notify(f"Konuşma tanıma servisine yeniden bağlanıldı; bekleyen {self._segments.qsize() + 1} "
                            "konuşma işleniyor.")
            if saved is not None:
                saved.unlink(missing_ok=True)
            self._publish(segment, result)
            return

    def _publish(self, segment: AudioSegment, result: SttResult | None) -> None:
        if result is None:
            self.stats.unintelligible += 1
            log.info("Anlaşılamayan ses (%.1f sn) atlandı.", segment.duration)
            return
        self.stats.recognized += 1
        self.stats.total_stt_latency += result.latency_s
        self.stats.max_stt_latency = max(self.stats.max_stt_latency, result.latency_s)
        text = choose_transcript(result, self.is_addressed)
        slow = result.latency_s >= self.slow_warning_s
        if slow:
            self.stats.slow += 1
        # Tanınan metin Faz 4'teki gibi yalnızca yerel günlüğe yazılır (logs/jarvis.log); başka yere gönderilmez.
        log.log(logging.WARNING if slow else logging.INFO,
                "STT %.2f sn%s (ses %.1f sn, %d Hz, hazırlık %.2f sn, bekleyen %d bölüt, güven %s): %s",
                result.latency_s, " YAVAŞ" if slow else "", result.audio_s or segment.duration,
                result.sent_rate or segment.sample_rate, result.prepare_s, self._segments.qsize(),
                result.confidence, text)
        self._results.put(Utterance(text, captured_at=segment.started_at, ended_at=segment.ended_at,
                                    confidence=result.confidence, stt_latency=result.latency_s,
                                    alternatives=tuple(result.alternatives), recognized_at=time.monotonic(),
                                    voice_ended_at=segment.voice_ended_at))

    def _save_pending(self, segment: AudioSegment) -> Path | None:
        if self.pending_dir is None:
            return None
        name = f"bekleyen_{segment.started_at:%Y%m%d_%H%M%S_%f}.wav"
        try:
            return segment.save_wav(self.pending_dir / name)
        except OSError:
            log.exception("Bekleyen ses dosyası yazılamadı")
            return None


# ---------------------------------------------------------------------- fabrika
def build_voice_listener(settings, speech, parser, microphone_index: int | None = None,
                         stt_engine: str | None = None) -> tuple[VoiceListener | None, str | None]:
    """Ayarlara göre sesli dinleyici kurar. Hata varsa (None, açıklama) döner; çağıran klavyeye geçer."""
    from .audio_capture import MicrophoneError, PyAudioSource, bluetooth_warning  # noqa: PLC0415
    from .stt import SttUnavailable, create_stt  # noqa: PLC0415

    try:
        stt = create_stt(stt_engine or settings.stt_engine, settings.stt_language, settings.stt_timeout_s,
                         settings.google_speech_key, resample_hz=settings.stt_resample_hz)
    except SttUnavailable as exc:
        return None, str(exc)
    index = microphone_index if microphone_index is not None else settings.microphone_index
    source = PyAudioSource(device_index=index)
    try:  # mikrofonun açılabildiğini baştan doğrula; açılamazsa açık hata ver
        source.open()
    except MicrophoneError as exc:
        return None, str(exc)
    finally:
        source.close()
    warning = bluetooth_warning(source.name)
    if warning:
        log.warning(warning)
        speech.say(warning, wait=False)
    guard_s = settings.tts_echo_guard_ms / 1000
    barge_in = None
    if getattr(settings, "barge_in_enabled", False) and getattr(speech, "precise_playback_events", False):
        from .barge_in import BargeInDetector  # noqa: PLC0415

        def stop_speaking(_heard: str) -> None:
            speech.cancel_all()
            note = getattr(speech, "note", None)
            if note is not None:
                note("(Sesli yanıt durduruldu; dinliyorum.)")

        barge_in = BargeInDetector(
            stt, stop_speaking, parser.is_addressed, lambda text: parser.split_wake_word(text)[1],
            current_text=lambda: getattr(speech, "current_text", ""),
            echo_margin=settings.barge_in_echo_margin, wake_word=settings.wake_word)
    listener = VoiceListener(
        source, stt, notify=lambda message: speech.say(message, wait=False), is_muted=lambda: speech.is_muted(guard_s),
        is_addressed=parser.is_addressed, segmenter_config=segmenter_config_from(settings),
        pending_dir=(settings.output_dir / "ses_bekleyen") if settings.save_pending_audio else None,
        slow_warning_s=settings.stt_slow_warning_s,
        keep_partial_on_mute=bool(getattr(speech, "precise_playback_events", False)), barge_in=barge_in,
    )
    return listener, None


def segmenter_config_from(settings) -> SegmenterConfig:
    """Ayarlardan (.env) bölütleme yapılandırması; canlı uygulama ve test araçları aynısını kullanır."""
    return SegmenterConfig(energy_threshold=float(settings.vad_threshold), silence_ms=settings.vad_silence_ms,
                           min_speech_ms=settings.vad_min_speech_ms, max_segment_s=settings.vad_max_segment_s,
                           tail_keep_ms=settings.vad_tail_keep_ms,
                           release_ratio=settings.vad_release_percent / 100)
