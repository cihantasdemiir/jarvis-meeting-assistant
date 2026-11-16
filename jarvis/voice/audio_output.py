"""Hoparlör çıkışı (Faz 5b). Ses ÜRETİMİNDEN ayrıdır: bu sınıf yalnızca hazır PCM'i çalar.

PyAudio (PortAudio) zaten mikrofon için kullanılıyor (requirements-voice.txt); ek paket gerekmez.

Oynatmanın gerçekten bittiği an: `finish()` içindeki `stop_stream()` PortAudio'da (Pa_StopStream)
kuyruktaki tüm ses tamponları çalınana kadar bekler. Yani `write()` dönmesi sesin çalındığı anlamına
gelmez; yankı koruması `finish()` döndükten sonra başlar. Bluetooth hoparlörlerde aygıtın kendi gecikmesi
buna eklenir (TTS_ECHO_GUARD_MS ile karşılanır).
"""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class AudioOutputError(RuntimeError):
    pass


class AudioOutput(Protocol):
    def start(self, sample_rate: int) -> None: ...

    def write(self, pcm: bytes) -> None: ...

    def finish(self) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


class PyAudioOutput:
    """Tek kanallı 16 bit çıkış. Akış işler arasında açık tutulur (her yanıtta aygıt açma gecikmesi olmaz)."""

    def __init__(self, device_index: int | None = None, frames_per_buffer: int = 1024):
        self.device_index = device_index
        self.frames_per_buffer = frames_per_buffer
        self._pa = None
        self._stream = None
        self._rate = 0

    def start(self, sample_rate: int) -> None:
        try:
            if self._stream is not None and self._rate != sample_rate:
                self.close()
            if self._stream is None:
                import pyaudio  # noqa: PLC0415

                if self._pa is None:
                    self._pa = pyaudio.PyAudio()
                self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=sample_rate, output=True,
                                             output_device_index=self.device_index,
                                             frames_per_buffer=self.frames_per_buffer)
                self._rate = sample_rate
            elif self._stream.is_stopped():
                self._stream.start_stream()
        except ImportError as exc:
            raise AudioOutputError("PyAudio kurulu değil (pip install -r requirements-voice.txt).") from exc
        except Exception as exc:
            self.close()
            raise AudioOutputError(f"Hoparlör açılamadı: {exc}") from exc

    def write(self, pcm: bytes) -> None:
        if self._stream is None:
            raise AudioOutputError("Hoparlör akışı açık değil.")
        try:
            self._stream.write(pcm)
        except Exception as exc:
            raise AudioOutputError(f"Ses çalınamadı: {exc}") from exc

    def finish(self) -> None:
        """Kuyruktaki ses çalınana kadar bekler (PortAudio Pa_StopStream)."""
        if self._stream is None:
            return
        try:
            if self._stream.is_stopped():
                return
            self._stream.stop_stream()
        except Exception as exc:
            self.close()
            raise AudioOutputError(f"Ses çıkışı durdurulamadı: {exc}") from exc

    def abort(self) -> None:
        """İptal: yazmayı bırakır; aygıt tamponunda kalan kısa parça (~40 ms) çalınıp durur."""
        try:
            self.finish()
        except AudioOutputError:
            log.warning("İptal sırasında ses çıkışı kapatıldı")

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
        self._rate = 0


def list_output_devices() -> list[tuple[int, str]]:
    """Kullanılabilir hoparlörler (TTS_OUTPUT_DEVICE için)."""
    try:
        import pyaudio  # noqa: PLC0415
    except ImportError:
        return []
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if int(info.get("maxOutputChannels", 0)) > 0:
                devices.append((index, str(info.get("name", ""))))
        return devices
    finally:
        pa.terminate()
