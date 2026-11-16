"""Ses testleri için taklitler: gerçek mikrofon, Google STT veya SAPI5 kullanılmaz."""
from __future__ import annotations

import math
import threading
from array import array
from collections.abc import Callable

from jarvis.voice.audio_capture import MicrophoneError
from jarvis.voice.stt import SttNetworkError, SttResult

RATE = 16000
FRAME = 480  # 30 ms


def silence(ms: int) -> list[bytes]:
    return [b"\x00\x00" * FRAME] * max(1, ms // 30)


def tone(ms: int, amplitude: int = 4000, hz: int = 220) -> list[bytes]:
    frames = []
    for f in range(max(1, ms // 30)):
        samples = array("h", [int(amplitude * math.sin(2 * math.pi * hz * (f * FRAME + i) / RATE))
                              for i in range(FRAME)])
        frames.append(samples.tobytes())
    return frames


def utterances(count: int, speech_ms: int = 600, gap_ms: int = 1000, lead_ms: int = 900) -> list[bytes]:
    """Kalibrasyon sessizliği + `count` adet konuşma (ton) + aralarında sessizlik."""
    frames = silence(lead_ms)
    for _ in range(count):
        frames += tone(speech_ms) + silence(gap_ms)
    return frames


class FakeSource:
    """Çerçeve listesini mikrofon gibi verir. `fail_at` çerçevesinde kopma, `fail_opens` kadar açılmama simüle eder."""

    def __init__(self, frames: list[bytes], fail_at: set[int] | None = None, fail_opens: int = 0,
                 block_at_end: threading.Event | None = None):
        self.frames = frames
        self.fail_at = set(fail_at or ())
        self.fail_opens = fail_opens
        self.block_at_end = block_at_end
        self.sample_rate = RATE
        self.name = "Test mikrofonu"
        self.position = 0
        self.opens = 0
        self.closes = 0

    def open(self) -> None:
        self.opens += 1
        if self.fail_opens > 0:
            self.fail_opens -= 1
            raise MicrophoneError("aygıt bulunamadı (test)")

    def read(self) -> bytes:
        if self.position in self.fail_at:
            self.fail_at.discard(self.position)
            raise MicrophoneError("aygıt bağlantısı koptu (test)")
        if self.position >= len(self.frames):
            if self.block_at_end is not None:
                self.block_at_end.wait(5)
            raise EOFError
        frame = self.frames[self.position]
        self.position += 1
        return frame

    def close(self) -> None:
        self.closes += 1


class FakeStt:
    """Bölütleri sırayla verilen metinlere çevirir. None = anlaşılamadı; Exception örneği = o hata."""

    name = "test"
    language = "tr-TR"

    def __init__(self, outputs: list, alternatives: dict[int, list[str]] | None = None,
                 on_call: Callable[[int], None] | None = None):
        self.outputs = list(outputs)
        self.alternatives = alternatives or {}
        self.calls = 0
        self.on_call = on_call
        self.segments = []

    def transcribe(self, segment):
        index = self.calls
        self.calls += 1
        if self.on_call:
            self.on_call(index)
        output = self.outputs.pop(0) if self.outputs else None
        if isinstance(output, Exception):
            raise output
        self.segments.append(segment)
        if output is None:
            return None
        alts = self.alternatives.get(len(self.segments) - 1, [output])
        return SttResult(text=output, alternatives=alts, confidence=0.9, engine="test", latency_s=0.05)


def network_error() -> SttNetworkError:
    return SttNetworkError("internet yok (test)")
