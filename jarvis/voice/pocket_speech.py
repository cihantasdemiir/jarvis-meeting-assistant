"""Pocket TTS Turkish ile sesli yanıt motoru (Faz 5b).

`SpeechEngine` (pyttsx3) ile aynı arayüzü sunar: `say`, `is_muted`, `is_speaking`, `enabled`,
`status_message`, `voice_name`, `stop`, ayrıca `wait_idle` ve `shutdown`.

İş parçacıkları ve sorumluluklar
--------------------------------
  * `tts-synth`: modeli açılışta BİR KEZ yükler, kısa bir ısınma üretimi yapar, sonra kuyruktaki yanıtları
    sırayla parça parça üretir (ses ÜRETİMİ).
  * `tts-play`: üretilen parçaları hoparlöre yazar (ses OYNATIMI). Oynatma başlangıcı ve gerçek bitişi
    (aygıt tamponu boşaldığında) yalnızca burada bilinir.

Kuyruk ve iptal kuralları
-------------------------
  * Yanıtlar geldiği sırayla ve tek tek seslendirilir; iki yanıt asla üst üste binmez.
  * `say()` çağıranı bekletmez (komut işleme ve dinleme durmaz). `blocking=True` ile kurulursa (demo,
    mikrofon kontrolü) ses bitene kadar bekler.
  * Bekleyen yanıt sayısı `max_queue`'yu aşarsa en ESKİ bekleyen (henüz başlamamış) yanıt seslendirilmez.
    Metin her durumda konsola yazılmıştır; hiçbir bilgi kaybolmaz.
  * `cancel_all()`: bekleyenleri atar, çalanı ~0,1 sn içinde keser. `shutdown(t)`: en fazla t saniye son
    yanıtların bitmesini bekler, sonra keser. `stop()`: hemen keser.

Mikrofon (yankı) koruması
-------------------------
  * `is_muted()` YALNIZCA hoparlör gerçekten çalarken ve çalma bittikten sonraki `guard_s` boyunca True'dur.
    Ses üretilirken (henüz çalmıyorken) mikrofon dinlemeye devam eder.
  * Bir yazma işlemi `stuck_after_s` saniyeden uzun sürerse (aygıt koptu/takıldı) mikrofon susturulmaz ve
    sesli yanıt kapatılır; takılan hoparlör mikrofonu sonsuza kadar kapatamaz. Üretim takılırsa da oynatma
    aynı sürede kesilir.
  * Gerçek akustik yankı engelleme (AEC) YOKTUR: asistan konuşurken söylenenler komut akışına girmez.
    Yalnızca "Jarvis, dur" için dar bir kesme yolu vardır (barge_in.py); Esc tuşu da `cancel_all()` çağırır.
"""
from __future__ import annotations

import collections
import itertools
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TextIO

from .audio_output import AudioOutput, AudioOutputError
from .pocket_backend import SpeechBackend, TtsUnavailable, to_pcm16
from .speech_text import to_speech_text

log = logging.getLogger(__name__)

_END = object()
WARM_UP_TEXT = "Hazırım."
_MAX_PIECE_S = 0.1  # iptal gecikmesi: hoparlöre en fazla 100 ms'lik parçalar yazılır
_OUTPUT_FAILURES_BEFORE_DISABLE = 3


@dataclass(eq=False)  # kimliğe göre karşılaştırılır (kümede tutulur)
class SpeechJob:
    id: int
    text: str  # konsolda gösterilen özgün metin (değiştirilmez)
    spoken: str  # seslendirilen kopya (normalleştirilmiş)
    created_at: float
    chunks: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=64))
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "queued"  # queued | generating | playing | done | cancelled | dropped | failed
    error: str = ""
    audio_s: float = 0.0
    first_chunk_at: float | None = None  # üretimin ilk parçası
    playback_started_at: float | None = None
    playback_ended_at: float | None = None


@dataclass
class SpeechStats:
    spoken: int = 0
    dropped: int = 0
    cancelled: int = 0
    failed: int = 0
    output_errors: int = 0


class PocketSpeechEngine:
    precise_playback_events = True  # susturma gerçek oynatmaya bağlı (VoiceListener yarım konuşmayı korur)

    def __init__(self, backend: SpeechBackend, output: AudioOutput | None = None, *, voice: str = "male_1",
                 volume: float = 1.0, console: TextIO | None = None, prefix: str = "JARVIS: ", max_queue: int = 5,
                 prebuffer_ms: int = 200, max_spoken_chars: int = 500, load_timeout_s: float = 120.0,
                 blocking: bool = False, stuck_after_s: float = 3.0, warm_up: bool = True, start: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.backend = backend
        self.output_device = output
        self.voice = voice
        self.volume = volume
        self.output = console or sys.stdout
        self.prefix = prefix
        self.max_queue = max(1, max_queue)
        self.prebuffer_ms = max(0, prebuffer_ms)
        self.max_spoken_chars = max_spoken_chars
        self.load_timeout_s = load_timeout_s
        self.blocking = blocking
        self.stuck_after_s = stuck_after_s
        self.warm_up = warm_up
        self.clock = clock
        self.enabled = True
        self.voice_name: str | None = None
        self.status_message = "Türkçe ses modeli yükleniyor..."
        self.last_error = ""
        self.load_seconds: float | None = None
        self.warmup_seconds: float | None = None
        self.stats = SpeechStats()
        self.on_speech_end: Callable[[], None] | None = None
        self.ready = threading.Event()
        self._ids = itertools.count(1)
        self._print_lock = threading.Lock()
        self._cond = threading.Condition()
        self._pending: collections.deque[SpeechJob] = collections.deque()
        self._unfinished: set[SpeechJob] = set()
        self._play_queue: queue.Queue[SpeechJob | None] = queue.Queue()
        self._state_lock = threading.Lock()
        self._playing = False
        self._current: SpeechJob | None = None  # şu an çalınan yanıt (kesme algılayıcısının yankı denetimi için)
        self._write_started: float | None = None
        self._last_play_end = float("-inf")
        self._stopping = threading.Event()
        self._output_failures = 0
        self._synth: threading.Thread | None = None
        self._player: threading.Thread | None = None
        if start:
            self.start()
            if not self.ready.wait(load_timeout_s):
                self.status_message = ("Türkçe ses modeli hâlâ yükleniyor; yüklenince sesli yanıt başlayacak. "
                                       "Bu sırada yanıtlar yalnızca konsola yazılır.")
                log.warning("TTS modeli %s sn içinde yüklenemedi; arka planda devam ediyor", load_timeout_s)

    # ================================================================ yaşam döngüsü
    def start(self) -> None:
        if self._synth is not None:
            return
        self._synth = threading.Thread(target=self._synth_main, daemon=True, name="tts-synth")
        self._player = threading.Thread(target=self._player_main, daemon=True, name="tts-play")
        self._synth.start()
        self._player.start()

    def _load(self) -> None:
        started = self.clock()
        self.backend.load()
        self.load_seconds = self.clock() - started
        voices = list(getattr(self.backend, "voices", []) or [])
        if voices and self.voice not in voices:
            # Sessizce başka sese geçilmez: kullanıcı male_1'i seçti.
            raise TtsUnavailable(f"TTS_VOICE='{self.voice}' modelde yok. Mevcut sesler: {', '.join(voices)}.")
        if self.warm_up:
            started = self.clock()
            for _ in self.backend.stream(WARM_UP_TEXT, self.voice):
                pass
            self.warmup_seconds = self.clock() - started
        description = getattr(self.backend, "description", "Pocket TTS Turkish")
        self.voice_name = f"{description} · {self.voice}"
        timing = f"model {self.load_seconds:.1f} sn'de yüklendi"
        if self.warmup_seconds is not None:
            timing += f", ısınma {self.warmup_seconds:.1f} sn"
        self.status_message = f"Türkçe ses: {self.voice_name} ({timing})."

    def _disable(self, message: str) -> None:
        self.enabled = False
        self.last_error = message
        self.status_message = message
        log.warning(message)
        self.cancel_all()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self.ready.wait(timeout)

    # ================================================================ dış arayüz
    @property
    def can_speak(self) -> bool:
        return self.enabled and self.ready.is_set() and not self._stopping.is_set()

    @property
    def is_speaking(self) -> bool:
        with self._cond:
            return bool(self._unfinished)

    @property
    def is_playing(self) -> bool:
        with self._state_lock:
            return self._playing

    @property
    def current_text(self) -> str:
        """Şu an hoparlörden çalınan yanıtın seslendirilen metni (yoksa boş)."""
        job = self._current
        return job.spoken if job is not None else ""

    def note(self, text: str) -> None:
        """Yalnızca konsola yazar; seslendirmez (ör. "sesli yanıt durduruldu")."""
        with self._print_lock:
            print(f"{self.prefix}{text}", file=self.output, flush=True)

    def say(self, text: str, wait: bool = True) -> SpeechJob | None:
        """Metni konsola yazar ve (ses hazırsa) seslendirme kuyruğuna ekler.

        Özgün metin değiştirilmez; seslendirilen kopya `to_speech_text` ile hazırlanır. Çağıranı yalnızca
        `blocking=True` iken ve `wait=True` ise ses bitene kadar bekletir.
        """
        if not text:
            return None
        with self._print_lock:
            print(f"{self.prefix}{text}", file=self.output, flush=True)
        if not self.can_speak:
            return None
        spoken = to_speech_text(text, self.max_spoken_chars)
        if not any(ch.isalnum() for ch in spoken):
            return None
        job = SpeechJob(next(self._ids), text, spoken, self.clock())
        with self._cond:
            if len(self._pending) >= self.max_queue:
                old = self._pending.popleft()
                self._finish(old, "dropped")
                log.warning("Ses kuyruğu dolu: bekleyen bir yanıt seslendirilmeden atlandı (metni konsolda).")
            self._pending.append(job)
            self._unfinished.add(job)
            self._cond.notify_all()
        if wait and self.blocking and threading.current_thread() not in (self._synth, self._player):
            job.done.wait(30 + len(spoken) / 5)
        return job

    def is_muted(self, guard_s: float = 0.7) -> bool:
        """Hoparlör çalıyorsa veya çalması `guard_s` saniye önce bittiyse True (yankı koruması)."""
        if not self.enabled:
            return False
        with self._state_lock:
            if self._playing:
                started = self._write_started
                if started is not None and self.clock() - started > self.stuck_after_s:
                    stuck = True
                else:
                    return True
            else:
                return self.clock() - self._last_play_end < guard_s
        if stuck:  # kilit dışında: _disable kuyruğu temizler
            self._disable("Hoparlör yanıt vermiyor (ses çıkışı takıldı); mikrofon yeniden dinliyor, yanıtlar "
                          "yalnızca konsola yazılacak.")
        return False

    def cancel_all(self) -> None:
        """Bekleyen yanıtları atar ve çalanı keser."""
        with self._cond:
            for job in list(self._pending):
                self._finish(job, "cancelled")
            self._pending.clear()
            for job in list(self._unfinished):
                job.cancel.set()
            self._cond.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Kuyruktaki tüm yanıtlar bitene (çalınıp hoparlör boşalana) kadar bekler."""
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            with self._cond:
                jobs = list(self._unfinished)
            if not jobs:
                return True
            remaining = None if deadline is None else deadline - self.clock()
            if remaining is not None and remaining <= 0:
                return False
            jobs[0].done.wait(0.1 if remaining is None else min(0.1, remaining))

    def shutdown(self, timeout: float = 15.0) -> bool:
        """Kapanış: son yanıtların bitmesini en fazla `timeout` sn bekler, sonra keser ve iş parçacıklarını kapatır."""
        finished = self.wait_idle(timeout) if self.can_speak else True
        if not finished:
            log.warning("Kapanışta seslendirme %s sn içinde bitmedi; kesildi", timeout)
        self.stop()
        return finished

    def stop(self) -> None:
        self._stopping.set()
        self.cancel_all()
        with self._cond:
            self._cond.notify_all()
        for thread in (self._player, self._synth):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(2.0)

    # ================================================================ üretim (tts-synth)
    def _synth_main(self) -> None:
        try:
            self._load()
        except TtsUnavailable as exc:
            self._disable(f"{exc} Yanıtlar yalnızca konsola yazılacak.")
        except Exception as exc:  # beklenmeyen kütüphane hatası uygulamayı düşürmez
            log.exception("Türkçe ses başlatılamadı")
            self._disable(f"Türkçe ses başlatılamadı ({exc}); yanıtlar yalnızca konsola yazılacak.")
        finally:
            self.ready.set()
        try:
            while self.enabled:
                job = self._next_job()
                if job is None:
                    break
                self._play_queue.put(job)
                self._generate(job)
        finally:
            self._play_queue.put(None)

    def _next_job(self) -> SpeechJob | None:
        with self._cond:
            while not self._pending:
                if self._stopping.is_set() or not self.enabled:
                    return None
                self._cond.wait(0.5)
            job = self._pending.popleft()
            job.status = "generating"
            return job

    def _generate(self, job: SpeechJob) -> None:
        max_piece = max(2, int(self.backend.sample_rate * _MAX_PIECE_S) * 2)
        try:
            stream = self.backend.stream(job.spoken, self.voice)
            try:
                for chunk in stream:
                    if job.cancel.is_set() or self._stopping.is_set():
                        break
                    if job.first_chunk_at is None:
                        job.first_chunk_at = self.clock()
                    pcm = to_pcm16(chunk, self.volume)
                    if not all(self._put(job, pcm[i:i + max_piece]) for i in range(0, len(pcm), max_piece)):
                        break
            finally:
                close = getattr(stream, "close", None)  # üretecin kapanması kütüphanede üretimi durdurur
                if close is not None:
                    close()
        except Exception as exc:
            job.error = f"Ses üretilemedi: {exc}"
            log.warning("Ses üretim hatası (yanıt konsolda): %s", exc)
        finally:
            self._put(job, _END, force=True)

    def _put(self, job: SpeechJob, item: object, force: bool = False) -> bool:
        """Parçayı oynatıcıya verir; oynatıcı yavaşsa bekler (bellek sınırlı kalır). İptalde False."""
        while True:
            if not force and (job.cancel.is_set() or self._stopping.is_set()):
                return False
            try:
                job.chunks.put(item, timeout=0.1)
                return True
            except queue.Full:
                # İptal edilmiş işte oynatıcı artık okumuyor olabilir: bitiş işareti beklemeden bırakılır.
                if force and (job.cancel.is_set() or self._player is None or not self._player.is_alive()):
                    return False

    # ================================================================ oynatma (tts-play)
    def _player_main(self) -> None:
        try:
            while True:
                job = self._play_queue.get()
                if job is None:
                    break
                self._play(job)
        finally:
            if self.output_device is not None:
                try:
                    self.output_device.close()
                except Exception:
                    log.exception("Ses çıkışı kapatılamadı")

    def _next_chunk(self, job: SpeechJob, timeout: float | None) -> object:
        """Sıradaki parça; `timeout` içinde gelmezse None (üretim takıldı)."""
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            try:
                return job.chunks.get(timeout=0.1)
            except queue.Empty:
                if deadline is not None and self.clock() > deadline:
                    return None
                if self._synth is not None and not self._synth.is_alive():
                    return _END

    def _play(self, job: SpeechJob) -> None:
        rate = int(self.backend.sample_rate)
        prebuffer_bytes = int(rate * self.prebuffer_ms / 1000) * 2
        buffered: list[bytes] = []
        size = 0
        ended = False
        try:
            while size < max(1, prebuffer_bytes):  # oynatmadan önce kısa tampon: mikrofon henüz susmaz
                item = self._next_chunk(job, None)
                if item is _END:
                    ended = True
                    break
                buffered.append(item)  # type: ignore[arg-type]
                size += len(item)  # type: ignore[arg-type]
            if job.cancel.is_set() or not buffered:
                self._finish(job, "cancelled" if job.cancel.is_set() else "failed")
                return
            if self.output_device is None:
                raise AudioOutputError("Ses çıkışı yapılandırılmadı.")
            self.output_device.start(rate)
            self._current = job
            self._set_playing(True)
            job.status = "playing"
            job.playback_started_at = self.clock()
            for piece in buffered:
                self._write(job, piece, rate)
            while not ended and not job.cancel.is_set():
                item = self._next_chunk(job, self.stuck_after_s)
                if item is None:
                    job.error = "Ses üretimi takıldı; yanıtın kalanı seslendirilmedi."
                    log.warning(job.error)
                    job.cancel.set()
                    break
                if item is _END:
                    ended = True
                    break
                self._write(job, item, rate)  # type: ignore[arg-type]
            if job.cancel.is_set():
                self._timed(self.output_device.abort)
            else:
                self._timed(self.output_device.finish)  # gerçek bitiş: aygıt tamponu boşaldı
            self._output_failures = 0
            self._finish(job, "cancelled" if job.cancel.is_set() else ("failed" if job.error else "done"))
        except AudioOutputError as exc:
            self._output_failures += 1
            self.stats.output_errors += 1
            job.error = str(exc)
            log.warning("Ses çıkışı hatası (yanıt konsolda): %s", exc)
            job.cancel.set()
            try:
                self.output_device.close() if self.output_device is not None else None
            except Exception:
                pass
            self._finish(job, "failed")
            if self.enabled and (self._output_failures >= _OUTPUT_FAILURES_BEFORE_DISABLE
                                 or self.output_device is None):
                self._disable(f"Hoparlör kullanılamıyor ({exc}); yanıtlar yalnızca konsola yazılacak.")
        except Exception as exc:
            log.exception("Beklenmeyen oynatma hatası")
            job.error = str(exc)
            job.cancel.set()
            self._finish(job, "failed")
        finally:
            self._set_playing(False)
            self._current = None
            self._discard_rest(job, ended)

    def _write(self, job: SpeechJob, pcm: bytes, rate: int) -> None:
        self._timed(lambda: self.output_device.write(pcm))  # type: ignore[union-attr]
        job.audio_s += len(pcm) / 2 / rate

    def _timed(self, action: Callable[[], None]) -> None:
        with self._state_lock:
            self._write_started = self.clock()
        try:
            action()
        finally:
            with self._state_lock:
                self._write_started = None
        if not self.enabled:  # takılma bekçisi bu sırada sesi kapattı
            raise AudioOutputError("ses çıkışı takıldı")

    def _set_playing(self, playing: bool) -> None:
        with self._state_lock:
            if self._playing and not playing:
                self._last_play_end = self.clock()
            self._playing = playing

    def _discard_rest(self, job: SpeechJob, ended: bool) -> None:
        """İptal/hata sonrası üreticinin bekleyen parçalarını boşaltır (üretici takılmasın)."""
        job.cancel.set()
        while not ended:
            item = self._next_chunk(job, 5.0)
            if item is _END or item is None:
                return

    def _finish(self, job: SpeechJob, status: str) -> None:
        if job.done.is_set():
            return
        job.status = status
        job.playback_ended_at = self.clock() if job.playback_started_at is not None else None
        if status == "done":
            self.stats.spoken += 1
        elif status == "dropped":
            self.stats.dropped += 1
        elif status == "cancelled":
            self.stats.cancelled += 1
        elif status == "failed":
            self.stats.failed += 1
        with self._cond:
            self._unfinished.discard(job)
        job.done.set()
        if self.on_speech_end is not None and job.playback_started_at is not None:
            try:
                self.on_speech_end()
            except Exception:
                log.exception("on_speech_end hatası")
