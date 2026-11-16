"""Pocket TTS testleri için sahte model ve sahte hoparlör (gerçek model, torch veya ses aygıtı gerekmez)."""
from __future__ import annotations

import threading
import time
import zlib

from jarvis.voice.audio_output import AudioOutputError
from jarvis.voice.pocket_backend import TtsUnavailable

RATE = 1000  # küçük örnekleme hızı: 100 ms = 200 bayt


def marker(text: str) -> int:
    return (zlib.crc32(text.encode("utf-8")) % 250) + 1


class FakeBackend:
    """`stream()` her metin için o metne özgü bayt değeriyle dolu PCM parçaları üretir (karışma testi için)."""

    def __init__(self, voices=("female_1", "male_1", "male_2"), load_error: Exception | None = None,
                 load_delay: float = 0.0, chunks: int = 3, chunk_ms: int = 100, gen_delay: float = 0.0,
                 fail_on: str | None = None, hang_on: str | None = None, hang_from: int = 0):
        self.sample_rate = RATE
        self.voices = list(voices)
        self.description = "Sahte Pocket"
        self.load_error = load_error
        self.load_delay = load_delay
        self.chunks = chunks
        self.chunk_ms = chunk_ms
        self.gen_delay = gen_delay
        self.fail_on = fail_on
        self.hang_on = hang_on
        self.hang_from = hang_from
        self.hang_release = threading.Event()
        self.load_calls = 0
        self.requests: list[tuple[str, str]] = []
        self.closed_early = 0

    def load(self) -> None:
        self.load_calls += 1
        time.sleep(self.load_delay)
        if self.load_error is not None:
            raise self.load_error

    def stream(self, text: str, voice: str):
        self.requests.append((text, voice))
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("model hatası")
        return self._gen(text)

    def _gen(self, text: str):
        size = int(RATE * self.chunk_ms / 1000) * 2
        finished = False
        try:
            for index in range(self.chunks):
                if self.hang_on and self.hang_on in text and index >= self.hang_from:
                    self.hang_release.wait(10)
                time.sleep(self.gen_delay)
                yield bytes([marker(text)]) * size
            finished = True
        finally:
            if not finished:
                self.closed_early += 1


class FakeOutput:
    """Yazılan sesi gerçek zamanlı "çalar" (speed < 1 hızlandırır). Olayları zaman damgasıyla kaydeder."""

    def __init__(self, speed: float = 0.1, fail_start: int = 0, fail_write_on: int | None = None,
                 block_write: threading.Event | None = None):
        self.speed = speed
        self.fail_start = fail_start
        self.fail_write_on = fail_write_on
        self.block_write = block_write
        self.events: list[tuple[str, float, object]] = []
        self.writes: list[bytes] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def _log(self, name, value=None):
        with self._lock:
            self.events.append((name, time.monotonic(), value))

    def start(self, sample_rate: int) -> None:
        if self.fail_start:
            self.fail_start -= 1
            raise AudioOutputError("aygıt yok")
        self._log("start", sample_rate)

    def write(self, pcm: bytes) -> None:
        if self.block_write is not None:
            self.block_write.wait(10)
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            index = len(self.writes)
            self.writes.append(pcm)
        try:
            if self.fail_write_on is not None and index == self.fail_write_on:
                raise AudioOutputError("çalma hatası")
            time.sleep(len(pcm) / 2 / RATE * self.speed)
        finally:
            with self._lock:
                self.active -= 1
        self._log("write", pcm[:1])

    def finish(self) -> None:
        self._log("finish")

    def abort(self) -> None:
        self._log("abort")

    def close(self) -> None:
        self._log("close")

    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]


def missing_model() -> TtsUnavailable:
    from jarvis.voice.pocket_backend import TtsModelMissing

    return TtsModelMissing("Türkçe ses modeli bu bilgisayarda bulunamadı. python main.py --tts-download")
