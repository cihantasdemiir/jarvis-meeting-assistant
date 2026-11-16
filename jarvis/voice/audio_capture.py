"""Mikrofon yakalama ve konuşma bölütleme (talimat Bölüm 6).

Mimari (MeetingScribe'ın kuyruk + tüketici iş parçacığı yaklaşımından uyarlanmıştır, bkz.
docs/SOURCE_AUDIT.md; kod kopyalanmamış, aynı fikir sıfırdan yazılmıştır):

    mikrofon ──► AudioCapture (TEK okuyucu iş parçacığı)
                     │  ham çerçeveler (30 ms, 16 bit mono)
                     ▼
                 Segmenter (enerji tabanlı konuşma algılama)
                     │  konuşma bölütleri (sessizlikte kesilir)
                     ▼
                 on_segment(...)  → VoiceListener kuyruğu → STT

MeetingScribe'dan farkları:
  * Sabit 5 sn pencere yerine sessizlikte biten bölütler: bir komut ikiye bölünmez.
  * Asistan konuşurken (TTS) çerçeveler atılır; asistanın kendi sesi komut sanılmaz.
  * Mikrofon koparsa iş parçacığı ölmez; kullanıcı bilgilendirilir ve yeniden bağlanılır.
  * Program durdurulurken yarım bölüt atılmaz, son bölüt olarak teslim edilir.
"""
from __future__ import annotations

import io
import logging
import math
import threading
import time
import wave
from array import array
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

SAMPLE_WIDTH = 2  # 16 bit
DEFAULT_RATE = 16000
FRAME_MS = 30


# ====================================================================== veri
@dataclass
class AudioSegment:
    """Tek bir konuşma bölütü (ham PCM, 16 bit mono)."""

    pcm: bytes
    sample_rate: int
    started_at: datetime
    ended_at: datetime
    sample_width: int = SAMPLE_WIDTH
    peak_rms: float = 0.0
    forced_cut: bool = False  # azami süre dolduğu için kesildi (konuşma devam ediyordu)
    final: bool = False  # program durdurulurken teslim edilen son bölüt
    # Konuşmanın gerçekten bittiği an (son sesli çerçeve). `ended_at` korunan sessizliği de içerir;
    # gecikme ölçümü (T-01) kullanıcının susduğu andan başlamalıdır.
    voice_ended_at: datetime | None = None

    @property
    def duration(self) -> float:
        return len(self.pcm) / (self.sample_rate * self.sample_width)

    def resampled(self, target_rate: int) -> "AudioSegment":
        """Bölütü başka bir örnekleme hızına çevirir (STT'ye gönderilen veriyi küçültmek için)."""
        if not target_rate or target_rate == self.sample_rate:
            return self
        pcm = resample_pcm16(self.pcm, self.sample_rate, target_rate)
        return AudioSegment(pcm=pcm, sample_rate=target_rate, started_at=self.started_at, ended_at=self.ended_at,
                            sample_width=self.sample_width, peak_rms=self.peak_rms, forced_cut=self.forced_cut,
                            final=self.final, voice_ended_at=self.voice_ended_at)

    def to_wav_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(self.sample_width)
            wav.setframerate(self.sample_rate)
            wav.writeframes(self.pcm)
        return buffer.getvalue()

    def save_wav(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_wav_bytes())
        return path


def frame_rms(frame: bytes) -> float:
    """16 bit PCM çerçevesinin RMS enerjisi (audioop Python 3.13'te kaldırıldığı için saf Python)."""
    if len(frame) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """16 bit mono PCM'i yeniden örnekler (saf Python, ek bağımlılık yok).

    Örnekleme hızı düşürülürken önce kaynak/hedef oranı genişliğinde kayan ortalama ile
    basit bir alçak geçiren süzgeç uygulanır (örtüşme/aliasing azalır), sonra doğrusal
    ara değerleme yapılır. 48000 → 16000 gibi tam katlarda bu, 3 örneğin ortalamasıdır.
    """
    if source_rate == target_rate or not pcm:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    count = len(samples)
    if count == 0:
        return b""
    ratio = source_rate / target_rate
    out = array("h")
    if ratio >= 1 and abs(ratio - round(ratio)) < 1e-9:
        step = int(round(ratio))
        for start in range(0, count - step + 1, step):
            out.append(int(sum(samples[start:start + step]) / step))
        return out.tobytes()
    source: list[float] | array = samples
    if ratio > 1:
        width = max(1, int(round(ratio)))
        prefix = [0]
        for value in samples:
            prefix.append(prefix[-1] + value)
        half = width // 2
        source = [(prefix[min(count, i + width - half)] - prefix[max(0, i - half)])
                  / (min(count, i + width - half) - max(0, i - half)) for i in range(count)]
    length = int(count / ratio)
    for index in range(length):
        position = index * ratio
        left = int(position)
        right = min(left + 1, count - 1)
        fraction = position - left
        value = source[left] * (1 - fraction) + source[right] * fraction
        out.append(max(-32768, min(32767, int(round(value)))))
    return out.tobytes()


_BLUETOOTH_HINTS = ("airpods", "hands-free", "handsfree", "hands free", "headset", "bluetooth", "buds", "bt ")


def bluetooth_warning(device_name: str) -> str | None:
    """Bluetooth kulaklık mikrofonları için uyarı metni (ad ipuçlarına göre; kesin tespit değildir)."""
    name = (device_name or "").lower()
    if not any(hint in name for hint in _BLUETOOTH_HINTS):
        return None
    return (f"Uyarı: '{device_name}' bir Bluetooth kulaklık mikrofonu gibi görünüyor. Windows bu aygıtlarda "
            "mikrofon açılınca dar bantlı 'Hands-Free' profiline geçer (8-16 kHz, sıkıştırılmış ses); bağlantı da "
            "kesilebilir. Bu, tanıma doğruluğunu belirgin biçimde düşürür. Toplantı için kablolu veya USB "
            "mikrofon kullanın.")


# ====================================================================== bölütleme
@dataclass
class SegmenterConfig:
    sample_rate: int = DEFAULT_RATE
    frame_ms: int = FRAME_MS
    energy_threshold: float = 0.0  # 0 = ortam gürültüsüne göre otomatik
    min_threshold: float = 120.0  # otomatik eşik bunun altına inmez
    threshold_multiplier: float = 3.0  # eşik = gürültü tabanı x çarpan
    calibration_ms: int = 600
    start_ms: int = 90  # konuşma başlangıcı için gereken kesintisiz ses süresi
    silence_ms: int = 800  # bu kadar sessizlik bölütü bitirir
    min_speech_ms: int = 300  # daha kısa sesler (öksürük, tıklama) atılır
    max_segment_s: float = 15.0
    preroll_ms: int = 300  # konuşma başlamadan önceki ses de eklenir (ilk hece kesilmesin)
    # Bölüt sonunda korunan "sessizlik". Türkçe kelime sonları (-lat, -tle, -de) çoğu zaman
    # eşiğin altında kalır; eskiden yalnızca 200 ms korunuyordu ve "toplantıyı başl" gibi
    # kesilmeler oluşuyordu (Faz 4 saha testi).
    tail_keep_ms: int = 500
    # Konuşma sürerken eşiğin bu oranı da konuşma sayılır (bitiş histerezisi). 0.75 → 0.5.
    release_ratio: float = 0.5

    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000) * SAMPLE_WIDTH

    def frames(self, ms: float) -> int:
        return max(1, int(round(ms / self.frame_ms)))


class Segmenter:
    """Çerçeve akışını konuşma bölütlerine ayırır. Saat dışarıdan verilir (test edilebilir)."""

    def __init__(self, config: SegmenterConfig | None = None):
        self.config = config or SegmenterConfig()
        c = self.config
        self._preroll: deque[bytes] = deque(maxlen=c.frames(c.preroll_ms))
        self._calibration: list[float] = []
        self._noise_floor: float | None = None
        self._threshold = c.energy_threshold or 0.0
        self._in_speech = False
        self._loud_run = 0
        self._silent_run = 0
        self._speech_frames = 0
        self._buffer: list[bytes] = []
        self._segment_start: datetime | None = None
        self._peak = 0.0
        self._quiet_needed = 0  # susturmadan sonra yeni bölüt için beklenecek sessiz çerçeve sayısı

    # ---------------------------------------------------------------- durum
    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def noise_floor(self) -> float | None:
        return self._noise_floor

    @property
    def calibrated(self) -> bool:
        return bool(self.config.energy_threshold) or self._noise_floor is not None

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def release_level(self) -> float:
        """Konuşma sürerken "hâlâ konuşuyor" sayılan en düşük enerji.

        Gürültü tabanının biraz üstünde tutulur; aksi halde gürültülü odada bölüt hiç bitmez.
        """
        level = self._threshold * self.config.release_ratio
        if self._noise_floor is not None:
            level = max(level, self._noise_floor * 1.3)
        return level

    def _frame_duration(self) -> timedelta:
        return timedelta(milliseconds=self.config.frame_ms)

    # ---------------------------------------------------------------- akış
    def feed(self, frame: bytes, now: datetime) -> list[AudioSegment]:
        """Bir çerçeve işler; tamamlanan bölütleri döndürür. `now` çerçevenin bitiş zamanıdır."""
        c = self.config
        rms = frame_rms(frame)
        if not self.calibrated:
            self._calibration.append(rms)
            self._preroll.append(frame)
            if len(self._calibration) >= c.frames(c.calibration_ms):
                self._set_noise_floor(sorted(self._calibration)[len(self._calibration) // 2])
            return []

        out: list[AudioSegment] = []
        loud = rms >= self._threshold
        if not self._in_speech:
            self._preroll.append(frame)
            if self._quiet_needed:
                # Susturma sırasında başlamış konuşmanın kuyruğu yeni bölüt sayılmaz.
                self._quiet_needed = c.frames(300) if loud else self._quiet_needed - 1
                return out
            if loud:
                self._loud_run += 1
                if self._loud_run >= c.frames(c.start_ms):
                    self._start_segment(now)
            else:
                self._loud_run = 0
                self._adapt_noise_floor(rms)
            return out

        self._buffer.append(frame)
        self._peak = max(self._peak, rms)
        # Histerezis: konuşma sırasında eşiğin altındaki zayıf kelime sonları da konuşma sayılır
        if rms >= self.release_level:
            self._silent_run = 0
            self._speech_frames += 1
        else:
            self._silent_run += 1
        if self._silent_run >= c.frames(c.silence_ms):
            segment = self._finish_segment(now, trim_silence=True)
            if segment:
                out.append(segment)
        elif len(self._buffer) * c.frame_ms >= c.max_segment_s * 1000:
            segment = self._finish_segment(now, trim_silence=False, forced=True)
            if segment:
                out.append(segment)
            # konuşma sürüyor: yeni bölüt hemen başlar
            self._start_segment(now, with_preroll=False)
        return out

    def flush(self, now: datetime) -> AudioSegment | None:
        """Program dururken yarım kalan konuşmayı teslim eder (tampon kaybolmaz)."""
        if not self._in_speech:
            return None
        segment = self._finish_segment(now, trim_silence=True)
        if segment:
            segment.final = True
        return segment

    def cut(self, now: datetime) -> AudioSegment | None:
        """Susturma başlarken o ana kadar söylenmiş yarım konuşmayı teslim eder (Faz 5b).

        Asistanın sesi bu bölüte girmez: susturma oynatma başlamadan hemen önce devreye girer.
        """
        if not self._in_speech:
            return None
        return self._finish_segment(now, trim_silence=True)

    def discard(self, require_quiet: bool = False) -> None:
        """Yarım bölütü atar (ör. asistan konuşmaya başladığında). Kalibrasyon korunur.

        require_quiet: yeni bölüt başlamadan önce kısa bir sessizlik beklenir; böylece
        asistan konuşurken başlayan sesin devamı yarım cümle olarak işlenmez.
        """
        if require_quiet:
            self._quiet_needed = self.config.frames(300)
        self._in_speech = False
        self._buffer = []
        self._loud_run = 0
        self._silent_run = 0
        self._speech_frames = 0
        self._segment_start = None
        self._preroll.clear()

    # ---------------------------------------------------------------- iç işler
    def _set_noise_floor(self, floor: float) -> None:
        c = self.config
        self._noise_floor = floor
        if not c.energy_threshold:
            self._threshold = max(c.min_threshold, floor * c.threshold_multiplier)
        log.info("Mikrofon kalibrasyonu: gürültü tabanı %.0f, eşik %.0f", floor, self._threshold)

    def _adapt_noise_floor(self, rms: float) -> None:
        """Sessiz anlarda gürültü tabanını yavaşça günceller (klima açılması vb.)."""
        if self.config.energy_threshold or self._noise_floor is None:
            return
        self._noise_floor = 0.995 * self._noise_floor + 0.005 * rms
        self._threshold = max(self.config.min_threshold, self._noise_floor * self.config.threshold_multiplier)

    def _start_segment(self, now: datetime, with_preroll: bool = True) -> None:
        c = self.config
        self._in_speech = True
        self._buffer = list(self._preroll) if with_preroll else []
        self._preroll.clear()
        self._segment_start = now - self._frame_duration() * len(self._buffer)
        self._silent_run = 0
        self._loud_run = 0
        self._speech_frames = c.frames(c.start_ms)
        self._peak = 0.0

    def _finish_segment(self, now: datetime, trim_silence: bool, forced: bool = False) -> AudioSegment | None:
        c = self.config
        frames = self._buffer
        voiced_frames = len(frames) - self._silent_run
        if trim_silence and self._silent_run:
            keep_tail = c.frames(c.tail_keep_ms)
            cut = max(0, self._silent_run - keep_tail)
            if cut:
                frames = frames[:-cut]
        started = self._segment_start or now
        voice_ended = started + timedelta(milliseconds=max(0, voiced_frames) * c.frame_ms)
        speech_ms = self._speech_frames * c.frame_ms
        peak = self._peak
        self._in_speech = False
        self._buffer = []
        self._silent_run = 0
        self._speech_frames = 0
        self._segment_start = None
        if speech_ms < c.min_speech_ms or not frames:
            return None
        pcm = b"".join(frames)
        ended = started + timedelta(milliseconds=len(frames) * c.frame_ms)
        return AudioSegment(pcm=pcm, sample_rate=c.sample_rate, started_at=started, ended_at=ended,
                            peak_rms=peak, forced_cut=forced, voice_ended_at=min(voice_ended, ended))


# ====================================================================== ses kaynakları
class FrameSource(Protocol):
    sample_rate: int
    name: str

    def open(self) -> None: ...

    def read(self) -> bytes: ...

    def close(self) -> None: ...


class MicrophoneError(RuntimeError):
    """Mikrofon açılamadı veya okunamadı."""


class PyAudioSource:
    """Windows/Linux/macOS mikrofonu (PyAudio). 16 kHz açılamazsa 48/44.1 kHz denenir."""

    RATES = (16000, 48000, 44100)

    def __init__(self, device_index: int | None = None, frame_ms: int = FRAME_MS):
        self.device_index = device_index
        self.frame_ms = frame_ms
        self.sample_rate = DEFAULT_RATE
        self.name = "varsayılan mikrofon"
        self._pa = None
        self._stream = None

    def open(self) -> None:
        try:
            import pyaudio  # noqa: PLC0415
        except ImportError as exc:
            raise MicrophoneError("PyAudio kurulu değil. Kurulum: pip install -r requirements-voice.txt") from exc
        self.close()
        self._pa = pyaudio.PyAudio()
        try:
            info = (self._pa.get_device_info_by_index(self.device_index) if self.device_index is not None
                    else self._pa.get_default_input_device_info())
        except (OSError, IOError) as exc:
            self.close()
            raise MicrophoneError(f"Mikrofon bulunamadı ({exc}). 'python main.py --list-mics' ile kontrol edin.") \
                from exc
        if int(info.get("maxInputChannels", 0)) < 1:
            self.close()
            raise MicrophoneError(f"Seçilen aygıt bir mikrofon değil: {info.get('name')}")
        self.name = str(info.get("name", self.name))
        last_error: Exception | None = None
        for rate in self.RATES:
            try:
                self._stream = self._pa.open(
                    format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=int(rate * self.frame_ms / 1000))
                self.sample_rate = rate
                return
            except (OSError, ValueError) as exc:
                last_error = exc
        self.close()
        raise MicrophoneError(f"Mikrofon açılamadı ({self.name}): {last_error}")

    def read(self) -> bytes:
        if self._stream is None:
            raise MicrophoneError("Mikrofon akışı açık değil.")
        frames = int(self.sample_rate * self.frame_ms / 1000)
        try:
            return self._stream.read(frames, exception_on_overflow=False)
        except (OSError, IOError) as exc:
            raise MicrophoneError(f"Mikrofon okunamadı: {exc}") from exc

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:  # aygıt zaten kopmuş olabilir
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


class WavFileSource:
    """WAV dosyasını mikrofon gibi okur (testler ve kayıtlı ses ile yeniden değerlendirme için)."""

    def __init__(self, path: Path, frame_ms: int = FRAME_MS, realtime: bool = False, tail_silence_ms: int = 1500):
        self.path = Path(path)
        self.frame_ms = frame_ms
        self.realtime = realtime
        self.tail_silence_ms = tail_silence_ms
        self.name = self.path.name
        self.sample_rate = DEFAULT_RATE
        self._data = b""
        self._pos = 0

    def open(self) -> None:
        with wave.open(str(self.path), "rb") as wav:
            if wav.getsampwidth() != SAMPLE_WIDTH or wav.getnchannels() != 1:
                raise MicrophoneError("WAV dosyası 16 bit mono olmalı.")
            self.sample_rate = wav.getframerate()
            self._data = wav.readframes(wav.getnframes())
        self._data += b"\x00\x00" * int(self.sample_rate * self.tail_silence_ms / 1000)
        self._pos = 0

    def read(self) -> bytes:
        size = int(self.sample_rate * self.frame_ms / 1000) * SAMPLE_WIDTH
        if self._pos >= len(self._data):
            raise EOFError
        chunk = self._data[self._pos:self._pos + size]
        self._pos += size
        if self.realtime:
            time.sleep(self.frame_ms / 1000)
        return chunk.ljust(size, b"\x00")

    def close(self) -> None:
        pass


def list_input_devices() -> tuple[list[tuple[int, str, int]], str | None]:
    """Giriş aygıtları: [(numara, ad, varsayılan örnekleme hızı)], hata mesajı."""
    try:
        import pyaudio  # noqa: PLC0415
    except ImportError:
        return [], "PyAudio kurulu değil. Kurulum: pip install -r requirements-voice.txt"
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if int(info.get("maxInputChannels", 0)) > 0:
                devices.append((index, str(info.get("name")), int(info.get("defaultSampleRate", 0))))
        return devices, None
    except Exception as exc:
        return [], f"Ses aygıtları listelenemedi: {exc}"
    finally:
        pa.terminate()


# ====================================================================== yakalama iş parçacığı
StatusCallback = Callable[[str], None]
SegmentCallback = Callable[[AudioSegment], None]


class AudioCapture(threading.Thread):
    """Mikrofonu okuyan TEK iş parçacığı. Komut ve transkript için ayrı okuyucu yoktur."""

    def __init__(self, source: FrameSource, on_segment: SegmentCallback, on_status: StatusCallback | None = None,
                 is_muted: Callable[[], bool] | None = None, config: SegmenterConfig | None = None,
                 clock: Callable[[], datetime] = datetime.now, max_backoff_s: float = 10.0,
                 keep_partial_on_mute: bool = False, barge_in=None):
        super().__init__(daemon=True, name="audio-capture")
        # True: susturma başladığında yarım kalan kullanıcı konuşması atılmaz, bölüt olarak teslim edilir.
        # Yalnızca susturmanın gerçek hoparlör oynatımına bağlı olduğu motorda (Pocket) açılır.
        self.keep_partial_on_mute = keep_partial_on_mute
        # Susturma penceresindeki çerçevelerin kopyası yalnızca "Jarvis, dur" algılayıcısına gider (barge_in.py).
        # Mikrofonu yine yalnızca bu iş parçacığı okur; çerçeveler komut/transkript akışına GİRMEZ.
        self.barge_in = barge_in
        self.source = source
        self.on_segment = on_segment
        self.on_status = on_status or (lambda message: None)
        self.is_muted = is_muted or (lambda: False)
        self.base_config = config or SegmenterConfig()
        self.segmenter: Segmenter | None = None
        self.clock = clock
        self.max_backoff_s = max_backoff_s
        self._stop_event = threading.Event()
        self.connected = threading.Event()
        self.ended = threading.Event()  # kaynak bitti (WAV) veya durduruldu
        self.frames_read = 0
        self.disconnects = 0
        self.last_error = ""

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def _make_segmenter(self) -> Segmenter:
        cfg = self.base_config
        config = SegmenterConfig(**{**cfg.__dict__, "sample_rate": self.source.sample_rate})
        return Segmenter(config)

    def _open_with_retry(self) -> bool:
        backoff = 1.0
        reported = False
        while not self._stop_event.is_set():
            try:
                self.source.open()
                self.connected.set()
                if self.segmenter is None or self.segmenter.config.sample_rate != self.source.sample_rate:
                    self.segmenter = self._make_segmenter()
                if reported:
                    self.on_status(f"Mikrofon yeniden bağlandı: {self.source.name}.")
                return True
            except (MicrophoneError, OSError) as exc:
                self.last_error = str(exc)
                if not reported:
                    self.on_status(f"Mikrofon kullanılamıyor: {exc} Yeniden denenecek.")
                    reported = True
                log.warning("Mikrofon açılamadı: %s", exc)
                self._stop_event.wait(backoff)
                backoff = min(self.max_backoff_s, backoff * 2)
        return False

    def run(self) -> None:
        try:
            if not self._open_with_retry():
                return
            was_muted = False
            while not self._stop_event.is_set():
                try:
                    frame = self.source.read()
                except EOFError:
                    break
                except (MicrophoneError, OSError) as exc:
                    self.disconnects += 1
                    self.connected.clear()
                    self.last_error = str(exc)
                    log.warning("Mikrofon koptu: %s", exc)
                    self._deliver(self.segmenter.flush(self.clock()) if self.segmenter else None)
                    self.on_status("Mikrofon bağlantısı koptu; yeniden bağlanmaya çalışılıyor. "
                                   "Toplantı kaydı korunuyor.")
                    self.source.close()
                    if not self._open_with_retry():
                        return
                    self.on_status(f"Dinlemeye devam ediliyor ({self.source.name}).")
                    continue
                self.frames_read += 1
                assert self.segmenter is not None
                if self.is_muted():
                    # Asistan konuşuyor: kendi sesini komut sanmaması için çerçeveler atılır.
                    if not was_muted:
                        if self.keep_partial_on_mute:
                            self._deliver(self.segmenter.cut(self.clock()))
                        self.segmenter.discard(require_quiet=True)
                        was_muted = True
                        self._barge_in("begin_window", self.source.sample_rate)
                    self._barge_in("feed", frame, self.clock())
                    continue
                if was_muted:
                    self._barge_in("end_window")
                was_muted = False
                for segment in self.segmenter.feed(frame, self.clock()):
                    self._deliver(segment)
        except Exception:
            log.exception("Ses yakalama iş parçacığında beklenmeyen hata")
            self.on_status("Ses yakalamada beklenmeyen hata oluştu; ayrıntı logs/jarvis.log dosyasında.")
        finally:
            if self.segmenter is not None:
                self._deliver(self.segmenter.flush(self.clock()))
            self.source.close()
            self.connected.clear()
            self.ended.set()

    def _barge_in(self, method: str, *args) -> None:
        if self.barge_in is None:
            return
        try:
            getattr(self.barge_in, method)(*args)
        except Exception:  # algılayıcı hatası mikrofon döngüsünü asla durdurmaz
            log.exception("Kesme algılayıcısı hatası (%s)", method)

    def _deliver(self, segment: AudioSegment | None) -> None:
        if segment is None:
            return
        try:
            self.on_segment(segment)
        except Exception:
            log.exception("Bölüt tüketicisi hata verdi")


@dataclass
class LevelReport:
    """Mikrofon seviye ölçümü (kurulum kontrolü için)."""

    device: str
    sample_rate: int
    noise_floor: float
    peak: float
    suggested_threshold: float
    levels: list[float] = field(default_factory=list)


def measure_levels(source: FrameSource, seconds: float, on_level: Callable[[float], None] | None = None) -> LevelReport:
    """Mikrofonu belirli süre okuyup gürültü tabanını ve önerilen eşiği hesaplar."""
    source.open()
    try:
        frames = int(seconds * 1000 / FRAME_MS)
        levels = []
        for _ in range(frames):
            try:
                rms = frame_rms(source.read())
            except EOFError:
                break
            levels.append(rms)
            if on_level:
                on_level(rms)
    finally:
        source.close()
    ordered = sorted(levels) or [0.0]
    floor = ordered[len(ordered) // 4]  # en sessiz çeyrek
    config = SegmenterConfig()
    return LevelReport(device=source.name, sample_rate=source.sample_rate, noise_floor=floor, peak=ordered[-1],
                       suggested_threshold=max(config.min_threshold, floor * config.threshold_multiplier),
                       levels=levels)
