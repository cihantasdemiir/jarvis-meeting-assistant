"""Pocket TTS Turkish performans ölçümü (`python main.py --tts-bench`, Faz 5b).

Ölçülenler birbirinden AYRI raporlanır:
  * model yükleme süresi (dosyadan belleğe; ses üretimi içermez),
  * ilk ses üretimi (soğuk: sesin hazırlanması dahil),
  * model yüklüyken sonraki üretimler: ilk parçaya kadar geçen süre ve toplam üretim süresi,
  * üretilen sesin süresi ve gerçek zaman faktörü (RTF = üretim süresi / ses süresi; < 1 gerçek zamandan hızlı),
  * yaklaşık RAM (süreç çalışma kümesi),
  * TTS üretimi sürerken komut işleme gecikmesi (aynı komutlar, boşta ve üretim sırasında).

Komut gecikmesi ölçümü geçici bir veritabanıyla yapılır (gerçek toplantı verisine dokunulmaz) ve yalnızca kural
tabanlı komut işlemeyi kapsar; Google STT ağ süresi ve LLM yanıtı dahil değildir.
"""
from __future__ import annotations

import io
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .pocket_backend import TtsUnavailable

SENTENCES = [
    ("kısa", "Toplantı başlatıldı."),
    ("orta", "Toplantı başlatıldı. Gündemde dört madde bulunuyor. İlk gündem maddesi üretim planlaması."),
    ("uzun", "Görev kaydedildi: Mehmet Öztürk raporu 22.10.2026 Perşembe gününe kadar hazırlayacak. Önceki "
             "toplantıdan devreden iki açık görev var. Sürenin %80'i doldu; bu madde için 15 dakika ayrılmıştı."),
]
COMMANDS = ["Jarvis gündemi oku", "Jarvis söz Ayşe'de", "Bakım takvimini tedarikçiyle netleştirmemiz gerekiyor.",
            "Jarvis süre durumu", "Jarvis kim konuşmadı", "Jarvis açık görevleri oku"]
# Önerilen hedefler (kullanıcı ortamında ölçülerek doğrulanmalı)
TARGET_FIRST_AUDIO_S = 0.8  # yüklü modelde ilk parçaya kadar (p95)
TARGET_RTF = 0.5  # akış takılmasın diye gerçek zamandan en az 2 kat hızlı (medyan)
TARGET_COMMAND_DELAY_MS = 50.0  # üretim sırasında komut gecikmesi artışı (p95)


# ---------------------------------------------------------------------- bellek
def process_memory_mb() -> tuple[float | None, float | None]:
    """(güncel, en yüksek) çalışma kümesi MB. Ölçülemezse None."""
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415
            from ctypes import wintypes  # noqa: PLC0415

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20
        except Exception:
            return None, None
        return None, None
    try:
        values = {}
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("VmRSS", "VmHWM"):
                values[key] = int(rest.split()[0]) / 1024
        return values.get("VmRSS"), values.get("VmHWM")
    except OSError:
        return None, None


# ---------------------------------------------------------------------- üretim ölçümü
@dataclass
class Generation:
    label: str
    run: int
    first_chunk_s: float
    total_s: float
    audio_s: float

    @property
    def rtf(self) -> float:
        return self.total_s / self.audio_s if self.audio_s else float("inf")


def _samples(chunk: Any) -> int:
    if isinstance(chunk, (bytes, bytearray)):
        return len(chunk) // 2
    size = getattr(chunk, "size", None)
    return int(size) if size is not None else len(chunk)


def measure_generation(backend, text: str, voice: str, label: str, run: int,
                       clock: Callable[[], float] = time.perf_counter, keep: list | None = None) -> Generation:
    started = clock()
    first = None
    samples = 0
    for chunk in backend.stream(text, voice):
        if first is None:
            first = clock() - started
        samples += _samples(chunk)
        if keep is not None:
            keep.append(chunk)
    total = clock() - started
    return Generation(label, run, first if first is not None else total, total, samples / backend.sample_rate)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))] if ordered else 0.0


# ---------------------------------------------------------------------- komut gecikmesi
def measure_command_latency(settings, repeats: int, busy: Callable[[], None] | None = None) -> list[float]:
    """Geçici veritabanında aynı komutları çalıştırır; `busy` verilirse arka planda TTS üretimi sürer."""
    from ..app import demo_settings  # noqa: PLC0415
    from ..assistant import JarvisAssistant  # noqa: PLC0415
    from ..meeting.manager import MeetingManager  # noqa: PLC0415
    from ..meeting.storage import MeetingRepository  # noqa: PLC0415
    from .command_parser import CommandParser  # noqa: PLC0415
    from .speech_engine import SpeechEngine  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="jarvis-tts-bench-", ignore_cleanup_errors=True) as tmp:
        bench = demo_settings(settings).with_overrides(db_path=Path(tmp) / "bench.db", output_dir=Path(tmp),
                                                       llm_enabled=False)
        bench.ensure_dirs()
        repo = MeetingRepository(bench.db_path)
        manager = MeetingManager(repo, bench, demo=True)
        assistant = JarvisAssistant(manager, CommandParser(bench.wake_word, bench.wake_word_aliases),
                                    SpeechEngine(enabled=False, output=io.StringIO()))
        assistant.handle_text("Jarvis toplantıyı başlat: TTS ölçümü")
        assistant.handle_text("Jarvis katılımcılar: Kenan, Ayşe, Mehmet")
        stop = threading.Event()
        worker = None
        if busy is not None:
            def loop() -> None:
                while not stop.is_set():
                    busy()
            worker = threading.Thread(target=loop, daemon=True, name="tts-bench-busy")
            worker.start()
            time.sleep(0.5)  # üretim başlasın
        latencies = []
        try:
            for _ in range(repeats):
                for command in COMMANDS:
                    started = time.perf_counter()
                    assistant.handle_text(command)
                    latencies.append((time.perf_counter() - started) * 1000)
        finally:
            stop.set()
            if worker is not None:
                worker.join(60)
    return latencies


# ---------------------------------------------------------------------- rapor
@dataclass
class BenchReport:
    device: str
    voice: str
    status: str = "tamamlandı"
    load_s: float | None = None
    cold: Generation | None = None
    warm: list[Generation] = field(default_factory=list)
    ram_before_mb: float | None = None
    ram_after_load_mb: float | None = None
    ram_peak_mb: float | None = None
    command_idle_ms: list[float] = field(default_factory=list)
    command_busy_ms: list[float] = field(default_factory=list)
    playback: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        firsts = [g.first_chunk_s for g in self.warm]
        rtfs = [g.rtf for g in self.warm]
        idle = self.command_idle_ms
        busy = self.command_busy_ms
        result: dict[str, Any] = {
            "first_audio_p50_s": statistics.median(firsts) if firsts else None,
            "first_audio_p95_s": _p95(firsts) if firsts else None,
            "rtf_median": statistics.median(rtfs) if rtfs else None,
            "rtf_max": max(rtfs) if rtfs else None,
            "command_idle_p95_ms": _p95(idle) if idle else None,
            "command_busy_p95_ms": _p95(busy) if busy else None,
        }
        if idle and busy:
            result["command_delay_increase_p95_ms"] = _p95(busy) - _p95(idle)
        result["targets"] = {
            "first_audio": _verdict(result["first_audio_p95_s"], TARGET_FIRST_AUDIO_S),
            "rtf": _verdict(result["rtf_median"], TARGET_RTF),
            "command_delay": _verdict(result.get("command_delay_increase_p95_ms"), TARGET_COMMAND_DELAY_MS),
        }
        return result


def _verdict(value: float | None, limit: float) -> str:
    if value is None:
        return "ölçülmedi"
    return "sağlandı" if value <= limit else "SAĞLANMADI"


def _fmt(value: float | None, unit: str = "", digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def render_markdown(report: BenchReport, note: str = "") -> str:
    s = report.summary()
    lines = [
        "# Pocket TTS Turkish performans ölçümü", "",
        f"Tarih: {datetime.now():%d.%m.%Y %H:%M}  ", f"Cihaz: **{report.device}**, ses: **{report.voice}**  ",
        f"Durum: {report.status}  ",
    ]
    if note:
        lines.append(f"Not: {note}  ")
    env = report.environment
    lines += ["", "## Ortam", "", "| Özellik | Değer |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in env.items()]
    lines += ["", "## Yükleme ve bellek (ses üretimi hariç)", "", "| Ölçüt | Değer |", "|---|---|",
              f"| Model yükleme | {_fmt(report.load_s, ' sn')} |",
              f"| RAM, yüklemeden önce | {_fmt(report.ram_before_mb, ' MB', 0)} |",
              f"| RAM, yüklemeden sonra | {_fmt(report.ram_after_load_mb, ' MB', 0)} |",
              f"| RAM, en yüksek | {_fmt(report.ram_peak_mb, ' MB', 0)} |"]
    if report.cold:
        c = report.cold
        lines += ["", "## İlk ses üretimi (soğuk: ses hazırlığı dahil)", "",
                  f"İlk parça {c.first_chunk_s:.2f} sn, toplam {c.total_s:.2f} sn, ses {c.audio_s:.2f} sn, "
                  f"RTF {c.rtf:.2f}"]
    lines += ["", "## Model yüklüyken üretimler", "",
              "| Cümle | Tekrar | İlk parça (sn) | Üretim (sn) | Ses (sn) | RTF |", "|---|---|---|---|---|---|"]
    lines += [f"| {g.label} | {g.run} | {g.first_chunk_s:.3f} | {g.total_s:.3f} | {g.audio_s:.2f} | {g.rtf:.3f} |"
              for g in report.warm]
    lines += ["", "## Komut işleme gecikmesi (kural tabanlı, geçici veritabanı)", "",
              "| Durum | Ölçüm | p50 (ms) | p95 (ms) |", "|---|---|---|---|"]
    for name, values in (("TTS boşta", report.command_idle_ms), ("TTS üretim yaparken", report.command_busy_ms)):
        if values:
            lines.append(f"| {name} | {len(values)} | {statistics.median(values):.1f} | {_p95(values):.1f} |")
    if report.playback:
        lines += ["", "## Hoparlör denemesi", "", "| Ölçüt | Değer |", "|---|---|"]
        lines += [f"| {key} | {value} |" for key, value in report.playback.items()]
    t = s["targets"]
    lines += ["", "## Önerilen hedefler", "", "| Hedef | Ölçülen | Sonuç |", "|---|---|---|",
              f"| İlk ses (yüklü model, p95) ≤ {TARGET_FIRST_AUDIO_S} sn | {_fmt(s['first_audio_p95_s'], ' sn')} | "
              f"{t['first_audio']} |",
              f"| RTF (medyan) ≤ {TARGET_RTF} | {_fmt(s['rtf_median'], '', 3)} | {t['rtf']} |",
              f"| Komut gecikmesi artışı (p95) ≤ {TARGET_COMMAND_DELAY_MS:.0f} ms | "
              f"{_fmt(s.get('command_delay_increase_p95_ms'), ' ms', 1)} | {t['command_delay']} |", "",
              "Hedefler öneridir. RTF < 1 gerçek zamandan hızlı demektir. Doğallık ve akıcılık bu ölçümle "
              "değerlendirilemez; dinleyerek değerlendirilmelidir."]
    if report.device == "cpu" and "SAĞLANMADI" in t.values():
        lines += ["", "**CPU hedefleri sağlanmadı.** Önce `TTS_CPU_THREADS` (2, 4) deneyin. GPU denemek için Qwen "
                  "çalışırken `nvidia-smi` ile boş VRAM'i kontrol edip `python main.py --tts-bench --tts-device cuda` "
                  "çalıştırın (CUDA'lı PyTorch gerekir). Ölçmeden varsayılanı GPU yapmayın."]
    return "\n".join(lines) + "\n"


def run_tts_benchmark(settings, runs: int = 3, play: bool = False, note: str = "", backend=None, output=None,
                      print_func: Callable[[str], None] = print, command_repeats: int = 10,
                      out_dir: Path | None = None) -> int:
    from .tts_factory import default_backend, default_output  # noqa: PLC0415

    report = BenchReport(device=settings.tts_device, voice=settings.tts_voice)
    report.environment = {"python": platform.python_version(), "sistem": f"{platform.system()} {platform.release()}",
                          "işlemci": platform.processor() or "—", "çekirdek": os.cpu_count() or "—",
                          "TTS_CPU_THREADS": settings.tts_cpu_threads or "varsayılan"}
    report.ram_before_mb, _ = process_memory_mb()
    backend = backend or default_backend(settings)
    print_func(f"Model yükleniyor ({settings.tts_device})...")
    started = time.perf_counter()
    try:
        backend.load()
    except TtsUnavailable as exc:
        print_func(f"HATA: {exc}")
        return 1
    report.load_s = time.perf_counter() - started
    report.ram_after_load_mb, _ = process_memory_mb()
    info = getattr(backend, "torch_info", None)
    if info is not None:
        report.environment.update({f"torch.{k}" if k in ("threads", "device") else k: v for k, v in info().items()})
    voices = list(getattr(backend, "voices", []) or [])
    if voices and settings.tts_voice not in voices:
        print_func(f"HATA: TTS_VOICE='{settings.tts_voice}' modelde yok. Mevcut: {', '.join(voices)}")
        return 1
    print_func(f"Yükleme {report.load_s:.2f} sn. İlk üretim (soğuk)...")
    kept: list = []
    report.cold = measure_generation(backend, SENTENCES[1][1], settings.tts_voice, SENTENCES[1][0], 0, keep=kept)
    for run in range(1, runs + 1):
        for label, text in SENTENCES:
            generation = measure_generation(backend, text, settings.tts_voice, label, run)
            report.warm.append(generation)
            print_func(f"  {label} #{run}: ilk parça {generation.first_chunk_s:.3f} sn, üretim "
                       f"{generation.total_s:.2f} sn, ses {generation.audio_s:.2f} sn, RTF {generation.rtf:.3f}")
    print_func("Komut gecikmesi ölçülüyor (boşta / TTS üretirken)...")
    report.command_idle_ms = measure_command_latency(settings, command_repeats)
    report.command_busy_ms = measure_command_latency(
        settings, command_repeats, busy=lambda: [None for _ in backend.stream(SENTENCES[2][1], settings.tts_voice)])
    if play:
        report.playback = _play(settings, backend, kept, output or default_output(settings), print_func)
    _, report.ram_peak_mb = process_memory_mb()
    if info is not None:  # GPU tepe belleği yalnızca üretimlerden SONRA anlamlı
        report.environment.update({k: v for k, v in info().items() if k.startswith("gpu")})
    target = out_dir or settings.output_dir / "tts_bench"
    target.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    md_path = target / f"tts_bench_{stamp}.md"
    md_path.write_text(render_markdown(report, note), encoding="utf-8")
    payload = {"report": asdict(report), "summary": report.summary(), "note": note}
    (target / f"tts_bench_{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                                                     encoding="utf-8")
    s = report.summary()
    print_func(f"\nİlk ses p95 {_fmt(s['first_audio_p95_s'], ' sn')} ({s['targets']['first_audio']}), "
               f"RTF medyan {_fmt(s['rtf_median'], '', 3)} ({s['targets']['rtf']}), komut gecikmesi artışı p95 "
               f"{_fmt(s.get('command_delay_increase_p95_ms'), ' ms', 1)} ({s['targets']['command_delay']}).")
    print_func(f"Rapor: {md_path}")
    return 0


def _play(settings, backend, chunks: list, output, print_func) -> dict[str, Any]:
    """Soğuk üretimde saklanan sesi hoparlörden çalar; çalma süresini ses süresiyle karşılaştırır."""
    from .audio_output import AudioOutputError  # noqa: PLC0415
    from .pocket_backend import to_pcm16  # noqa: PLC0415

    pcm = b"".join(to_pcm16(chunk, settings.tts_volume) for chunk in chunks)
    audio_s = len(pcm) / 2 / backend.sample_rate
    print_func(f"Hoparlörden çalınıyor ({audio_s:.1f} sn)...")
    try:
        output.start(backend.sample_rate)
        started = time.perf_counter()
        piece = backend.sample_rate // 10 * 2
        for i in range(0, len(pcm), piece):
            output.write(pcm[i:i + piece])
        written = time.perf_counter() - started
        output.finish()
        finished = time.perf_counter() - started
    except AudioOutputError as exc:
        print_func(f"HATA: {exc}")
        return {"durum": f"çalınamadı: {exc}"}
    finally:
        output.close()
    return {"durum": "çalındı (dinleyerek doğrulayın)", "ses süresi": f"{audio_s:.2f} sn",
            "yazma bitti": f"{written:.2f} sn", "hoparlör boşaldı": f"{finished:.2f} sn"}
