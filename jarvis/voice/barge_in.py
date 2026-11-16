"""Asistan konuşurken "Jarvis, dur" ile sesi kesme (barge-in).

Neden ayrı bir yol?
  Yankı koruması (Faz 5b), hoparlör çalarken mikrofon çerçevelerini atar. Böylece asistanın kendi sesi komut
  ya da onay sanılmaz. Ama bu, konuşma sırasında söylenen "Jarvis, dur"un da duyulmaması demektir.

Bu modül o pencerede, normal komut akışına HİÇ girmeyen dar bir yol açar:
  * Mikrofonu yine yalnızca `AudioCapture` okur. Susturma penceresindeki çerçeveler buraya kopyalanır;
    ikinci bir mikrofon açılmaz.
  * Ayrı ve kısa bir bölütleyici kullanılır. Her oynatma başında, o anki yankı düzeyine göre yeniden
    kalibre edilir: kullanıcının sesi yankıdan belirgin biçimde yüksek olmalıdır.
  * Bölüt kısa olmalıdır (en fazla `max_segment_s`). Uzun ses, büyük olasılıkla asistanın kendi sesidir.
  * Bölüt tek bir iş parçacığında tanınır. Aynı anda en fazla bir istek gider; meşgulken gelen bölüt atılır.
  * Tanınan metin YALNIZCA şu biçimdeyse kabul edilir: hitap + durdurma sözcüğü ("Jarvis dur / sus / yeter /
    dinle"), toplam en fazla 4 sözcük. Bu durumda tek etki seslendirmenin kesilmesidir.
  * Tanınan metin, o an seslendirilen cümlede de geçiyorsa (asistanın kendi sesi) yok sayılır.
  * Bu pencerede tanınan başka HİÇBİR metin asistana iletilmez. "evet", "onaylıyorum" gibi yankılar hiçbir kayıt
    işlemini tetikleyemez; en kötü durumda sesi gereksiz yere keser.

Sınırlar (dürüst durum):
  * Gerçek akustik yankı engelleme (AEC) yoktur. Hoparlör mikrofona yakın ve yüksek sesliyse kullanıcının
    sesi eşiği aşamayabilir. Bu durumda Esc tuşu kullanılır; kulaklık önerilir.
  * Google STT gecikmesi (~1-2 sn) nedeniyle kesme anlık değildir.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..utils.turkish_text import tr_lower
from .audio_capture import AudioSegment, Segmenter, SegmenterConfig
from .stt import SpeechToText, SttError

log = logging.getLogger(__name__)

STOP_WORDS = ("dur", "sus", "yeter", "dinle", "kes", "sessiz ol", "dur artık", "tamam dur", "tamam yeter")
_WORD = re.compile(r"\w+")


def is_stop_command(text: str, is_addressed: Callable[[str], bool], strip_wake: Callable[[str], str]) -> bool:
    """Yalnızca "Jarvis, dur" biçimi (hitap zorunlu, en fazla 4 sözcük). Başka hiçbir şey kabul edilmez."""
    if not text or not is_addressed(text):
        return False
    words = _WORD.findall(tr_lower(text))
    if len(words) > 4:
        return False
    rest = " ".join(_WORD.findall(tr_lower(strip_wake(text))))
    return rest in STOP_WORDS


@dataclass
class BargeInStats:
    windows: int = 0  # oynatma (susturma) penceresi
    segments: int = 0  # STT'ye giden kısa bölüt
    skipped_busy: int = 0  # tanıyıcı meşgulken atılan bölüt
    recognized: int = 0
    stops: int = 0
    echo_rejected: int = 0  # asistanın kendi cümlesiyle örtüşen tanıma
    other_rejected: int = 0  # durdurma komutu olmayan tanıma (asistana İLETİLMEDİ)


class BargeInDetector:
    def __init__(self, stt: SpeechToText, on_stop: Callable[[str], None],
                 is_addressed: Callable[[str], bool], strip_wake: Callable[[str], str],
                 current_text: Callable[[], str] | None = None, base_threshold: Callable[[], float] | None = None,
                 echo_margin: float = 2.5, max_segment_s: float = 3.0, max_requests_per_window: int = 4,
                 wake_word: str = "jarvis"):
        self.stt = stt
        self.on_stop = on_stop
        self.is_addressed = is_addressed
        self.strip_wake = strip_wake
        self.current_text = current_text or (lambda: "")
        self.base_threshold = base_threshold or (lambda: 0.0)
        self.echo_margin = echo_margin
        self.max_segment_s = max_segment_s
        self.max_requests_per_window = max_requests_per_window
        self.wake_word = " ".join(_WORD.findall(tr_lower(wake_word))) or "jarvis"
        self.stats = BargeInStats()
        self._segmenter: Segmenter | None = None
        self._sample_rate = 16000
        self._requests = 0
        self._stopped_this_window = False
        self._queue: queue.Queue[tuple[AudioSegment, str] | None] = queue.Queue(maxsize=1)
        self._worker = threading.Thread(target=self._run, daemon=True, name="barge-in-stt")
        self._closed = threading.Event()
        self._started = False

    # ---------------------------------------------------------------- yaşam döngüsü
    def start(self) -> None:
        if not self._started:
            self._started = True
            self._worker.start()

    def close(self, timeout: float = 2.0) -> None:
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._started and self._worker.is_alive():
            self._worker.join(timeout)

    # ---------------------------------------------------------------- AudioCapture iş parçacığından çağrılır
    def begin_window(self, sample_rate: int) -> None:
        """Hoparlör çalmaya başladı: kısa bölütleyici yankı düzeyine göre yeniden kalibre edilir."""
        self._sample_rate = sample_rate
        self._segmenter = Segmenter(SegmenterConfig(
            sample_rate=sample_rate, energy_threshold=0.0, min_threshold=max(120.0, self.base_threshold()),
            threshold_multiplier=self.echo_margin, calibration_ms=300, start_ms=120, silence_ms=450,
            min_speech_ms=250, max_segment_s=self.max_segment_s, preroll_ms=200, tail_keep_ms=300))
        self._requests = 0
        self._stopped_this_window = False
        self.stats.windows += 1

    def end_window(self) -> None:
        self._segmenter = None

    def feed(self, frame: bytes, now: datetime) -> None:
        segmenter = self._segmenter
        if segmenter is None or self._stopped_this_window or self._closed.is_set():
            return
        for segment in segmenter.feed(frame, now):
            if segment.duration > self.max_segment_s + 0.2:
                continue  # uzun ses: büyük olasılıkla asistanın kendi konuşması
            self._submit(segment)

    def _submit(self, segment: AudioSegment) -> None:
        if self._requests >= self.max_requests_per_window:
            return
        try:
            self._queue.put_nowait((segment, self.current_text()))
            self._requests += 1
            self.stats.segments += 1
        except queue.Full:
            self.stats.skipped_busy += 1

    # ---------------------------------------------------------------- tanıma iş parçacığı
    def _run(self) -> None:
        while not self._closed.is_set():
            item = self._queue.get()
            if item is None:
                return
            segment, playing_text = item
            try:
                result = self.stt.transcribe(segment)
            except SttError as exc:
                log.info("Kesme tanıma hatası (yok sayıldı): %s", exc)
                continue
            except Exception:
                log.exception("Kesme tanımada beklenmeyen hata (yok sayıldı)")
                continue
            if result is None:
                continue
            self.stats.recognized += 1
            self.evaluate([result.text, *result.alternatives[1:3]], playing_text)

    def evaluate(self, texts: list[str], playing_text: str) -> bool:
        """Tanınan metin(ler)i değerlendirir. True: seslendirme kesildi. Başka hiçbir yan etkisi yoktur."""
        spoken = " ".join(_WORD.findall(tr_lower(playing_text)))
        for text in texts:
            if not is_stop_command(text, self.is_addressed, self.strip_wake):
                continue
            rest = " ".join(_WORD.findall(tr_lower(self.strip_wake(text))))
            # Asistanın kendi cümlesi "«Jarvis, dur» deyin" içeriyorsa (STT hitabı "Carvis" diye duysa bile) yankıdır
            if spoken and f" {self.wake_word} {rest} " in f" {spoken} ":
                self.stats.echo_rejected += 1  # asistanın kendi cümlesi ("…Jarvis, dur deyin…")
                log.info("Kesme komutu asistanın kendi sesine benzediği için yok sayıldı")
                return False
            self._stopped_this_window = True
            self.stats.stops += 1
            log.info("Sesli kesme algılandı; seslendirme durduruluyor")
            try:
                self.on_stop(text)
            except Exception:
                log.exception("Kesme işleyicisi hata verdi")
            return True
        self.stats.other_rejected += 1  # bilinçli olarak hiçbir yere iletilmez
        return False
