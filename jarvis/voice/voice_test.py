"""Sesli komut doğruluk ölçümü (PDF Hafta 2, hedef ≥ %85) ve mikrofon kurulum kontrolü.

Komutlar:
    python main.py --mic-check                 # mikrofon seviyesi + tek cümle STT + Türkçe TTS denemesi
    python main.py --voice-test                # test setini gerçek mikrofonla okur, rapor yazar
    python main.py --voice-test --save-audio   # ayrıca her kaydı WAV olarak saklar
    python main.py --voice-test-replay KLASÖR  # saklanan kayıtları yeniden değerlendirir (ör. başka STT motoru)

Ölçüm dürüstlüğü:
  * Doğruluk yalnızca gerçekten söylenen ve tanınan kayıtlar üzerinden hesaplanır; atlanan
    maddeler paydaya girmez ve raporda ayrıca gösterilir.
  * Rapor; bilgisayar adı, mikrofon, STT motoru ve tarih bilgisini içerir.
  * Hiçbir sonuç elle düzeltilmez; ham STT metni rapora olduğu gibi yazılır.
"""
from __future__ import annotations

import csv
import json
import platform
import queue
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..utils.turkish_text import ascii_fold
from .audio_capture import AudioCapture, AudioSegment, FrameSource, SegmenterConfig, WavFileSource, bluetooth_warning
from .command_parser import CommandParser, Intent
from .stt import SpeechToText, SttNetworkError, SttResult
from .truncation import detect_truncation
from .voice_listener import choose_transcript

TEST_SET_PATH = Path(__file__).with_name("voice_test_set.json")
TARGET_ACCURACY = 85.0
TARGET_LATENCY_S = 5.0


# ====================================================================== test seti ve değerlendirme
@dataclass
class VoiceTestItem:
    say: str
    intents: list[str] | None  # None: komut değil (normal konuşma)
    params: dict[str, str] = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        return self.intents is not None


def load_test_set(path: Path = TEST_SET_PATH) -> list[VoiceTestItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for raw in data["items"]:
        intent = raw.get("intent")
        intents = None if intent is None else ([intent] if isinstance(intent, str) else list(intent))
        items.append(VoiceTestItem(say=raw["say"], intents=intents, params=dict(raw.get("params") or {})))
    return items


@dataclass
class Evaluation:
    detected: str  # niyet adı veya "—" (komut algılanmadı)
    addressed: bool
    correct: bool
    reason: str = ""


def _param_text(intent: Intent, key: str) -> str:
    value = intent.params.get(key)
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    return ascii_fold(str(value)) if value is not None else ""


def evaluate(item: VoiceTestItem, transcript: str | None, parser: CommandParser) -> Evaluation:
    """Tanınan metnin beklenen sonucu verip vermediğini ölçer."""
    if transcript is None:
        return Evaluation("—", False, False, "ses anlaşılamadı")
    intent = parser.parse(transcript)
    addressed = intent is not None
    if not item.is_command:
        if addressed:
            return Evaluation(intent.type.value, True, False, "normal konuşma komut sanıldı (yanlış tetikleme)")
        return Evaluation("—", False, True)
    if intent is None:
        first = transcript.split()[0] if transcript.split() else ""
        return Evaluation("—", False, False, f"uyandırma kelimesi algılanmadı (ilk kelime: '{first}')")
    if intent.type.value not in (item.intents or []):
        reason = f"beklenen: {'/'.join(item.intents or [])}"
        if intent.type.value == "UNKNOWN" and detect_truncation(str(intent.params.get("command", ""))):
            reason = f"komut kesilmiş (son kelime eksik); {reason}"
        return Evaluation(intent.type.value, True, False, reason)
    for key, expected in item.params.items():
        if ascii_fold(expected) not in _param_text(intent, key):
            return Evaluation(intent.type.value, True, False, f"'{key}' parametresi yanlış: "
                                                              f"{intent.params.get(key)!r}")
    return Evaluation(intent.type.value, True, True)


def evaluate_result(item: VoiceTestItem, transcript: str | None, parser: CommandParser,
                    error: str = "") -> Evaluation:
    """evaluate() + STT hatası. Ağ hatası "ses anlaşılamadı" diye raporlanmaz."""
    if transcript is None and error:
        return Evaluation("—", False, False, f"STT/ağ hatası (başarısız sayıldı): {error}")
    return evaluate(item, transcript, parser)


@dataclass
class ItemResult:
    number: int
    item: VoiceTestItem
    transcript: str | None
    alternatives: list[str]
    evaluation: Evaluation
    stt_latency: float | None
    end_to_intent: float | None  # konuşma sonundan niyet belirlenene kadar
    audio_file: str = ""
    error: str = ""
    extra_segments: int = 0  # aynı madde için gelen ek bölütler (komut ikiye bölünmüş olabilir)

    @property
    def network_error(self) -> bool:
        return bool(self.error) and self.transcript is None

    @property
    def truncated(self) -> bool:
        return self.evaluation.reason.startswith("komut kesilmiş")


@dataclass
class Summary:
    total_commands: int
    correct_commands: int
    wake_detected: int
    total_speech: int
    false_triggers: int
    unintelligible: int
    skipped: int
    avg_stt_latency: float
    max_end_to_intent: float
    network_errors: int = 0
    truncated: int = 0
    slow_stt: int = 0
    max_stt_latency: float = 0.0
    split_segments: int = 0

    @property
    def command_accuracy(self) -> float:
        return 100.0 * self.correct_commands / self.total_commands if self.total_commands else 0.0

    @property
    def wake_rate(self) -> float:
        return 100.0 * self.wake_detected / self.total_commands if self.total_commands else 0.0


def summarize(results: list[ItemResult], skipped: int, slow_warning_s: float = 4.0) -> Summary:
    commands = [r for r in results if r.item.is_command]
    speech = [r for r in results if not r.item.is_command]
    latencies = [r.stt_latency for r in results if r.stt_latency is not None]
    ends = [r.end_to_intent for r in results if r.end_to_intent is not None]
    return Summary(
        total_commands=len(commands),
        correct_commands=sum(r.evaluation.correct for r in commands),
        wake_detected=sum(r.evaluation.addressed for r in commands),
        total_speech=len(speech),
        false_triggers=sum(not r.evaluation.correct for r in speech),
        unintelligible=sum(r.transcript is None and not r.network_error for r in results),
        skipped=skipped,
        avg_stt_latency=sum(latencies) / len(latencies) if latencies else 0.0,
        max_end_to_intent=max(ends) if ends else 0.0,
        network_errors=sum(r.network_error for r in results),
        truncated=sum(r.truncated for r in results),
        slow_stt=sum(lat >= slow_warning_s for lat in latencies),
        max_stt_latency=max(latencies) if latencies else 0.0,
        split_segments=sum(r.extra_segments > 0 for r in results),
    )


def suggested_aliases(results: list[ItemResult], parser: CommandParser) -> list[str]:
    """Uyandırma kelimesinin yanlış tanındığı biçimler (WAKE_WORD_ALIASES için öneri)."""
    found = []
    for r in results:
        if r.item.is_command and r.transcript and not r.evaluation.addressed:
            first = r.transcript.split()[0].strip(" ,.:;!?").lower()
            rest = " ".join(r.transcript.split()[1:])
            if first and parser.parse_command(rest).type.value in (r.item.intents or []) and first not in found:
                found.append(first)
    return found


# ====================================================================== rapor
def write_report(results: list[ItemResult], skipped: int, output_dir: Path, context: dict[str, str],
                 parser: CommandParser, slow_warning_s: float = 4.0) -> tuple[Path, Path, Summary]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = summarize(results, skipped, slow_warning_s)
    csv_path = output_dir / f"dogruluk_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["No", "Söylenen", "STT çıktısı", "Beklenen", "Bulunan", "Sonuç", "Açıklama",
                         "STT süresi (sn)", "Konuşma sonu → niyet (sn)", "Ses dosyası", "Ek bölüt"])
        for r in results:
            writer.writerow([r.number, r.item.say, r.transcript or "(anlaşılamadı)",
                             "/".join(r.item.intents) if r.item.intents else "komut değil",
                             r.evaluation.detected, "Doğru" if r.evaluation.correct else "Yanlış",
                             r.evaluation.reason or r.error,
                             f"{r.stt_latency:.2f}" if r.stt_latency is not None else "",
                             f"{r.end_to_intent:.2f}" if r.end_to_intent is not None else "", r.audio_file,
                             r.extra_segments])

    md_path = output_dir / f"dogruluk_{stamp}.md"
    aliases = suggested_aliases(results, parser)
    passed = summary.command_accuracy >= TARGET_ACCURACY
    lines = [
        "# Sesli Komut Doğruluk Raporu", "",
        f"- Tarih: {datetime.now():%d.%m.%Y %H:%M}",
        f"- Bilgisayar: {context.get('machine', platform.node())} ({platform.system()} {platform.release()})",
        f"- Mikrofon: {context.get('device', '?')}",
        f"- STT motoru: {context.get('engine', '?')} ({context.get('language', 'tr-TR')})",
        f"- Kaynak: {context.get('source', 'gerçek mikrofon')}",
        f"- Ortam notu: {context.get('note') or '—'}", "",
        "## Özet", "",
        "| Ölçüt | Sonuç | Hedef |", "|---|---|---|",
        f"| Komut doğruluğu | **%{summary.command_accuracy:.1f}** "
        f"({summary.correct_commands}/{summary.total_commands}) "
        f"| ≥ %{TARGET_ACCURACY:.0f} — {'GEÇTİ' if passed else 'KALDI'} |",
        f"| Uyandırma kelimesi algılama | %{summary.wake_rate:.1f} "
        f"({summary.wake_detected}/{summary.total_commands}) | — |",
        f"| Yanlış tetikleme (normal konuşma komut sanıldı) | {summary.false_triggers}/{summary.total_speech} | 0 |",
        f"| Anlaşılamayan kayıt | {summary.unintelligible} | — |",
        f"| STT/ağ hatası (başarısız sayıldı) | {summary.network_errors} | 0 |",
        f"| Kesilmiş komut (son kelime eksik) | {summary.truncated} | 0 |",
        f"| İkiye bölünen konuşma (ek bölüt geldi) | {summary.split_segments} | 0 |",
        f"| Yavaş STT (≥ {slow_warning_s:.1f} sn) | {summary.slow_stt} | 0 |",
        f"| En uzun STT süresi | {summary.max_stt_latency:.2f} sn | — |",
        f"| Atlanan madde (ölçüme dahil değil) | {summary.skipped} | — |",
        f"| Ortalama STT süresi | {summary.avg_stt_latency:.2f} sn | — |",
        f"| En uzun konuşma sonu → niyet süresi | {summary.max_end_to_intent:.2f} sn "
        f"| ≤ {TARGET_LATENCY_S:.0f} sn (T-01) |",
        "",
    ]
    if aliases:
        lines += ["## Önerilen uyandırma kelimesi varyantları", "",
                  "Aşağıdaki biçimler 'Jarvis' yerine tanındı. `.env` içindeki `WAKE_WORD_ALIASES` satırına ekleyip "
                  "testi tekrarlayın:", "", f"`{','.join(aliases)}`", ""]
    lines += ["## Ayrıntılar", "",
              "| No | Söylenen | STT çıktısı | Beklenen | Bulunan | Sonuç | Açıklama | STT (sn) |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        expected = "/".join(r.item.intents) if r.item.intents else "komut değil"
        stt = f"{r.stt_latency:.2f}" if r.stt_latency is not None else ""
        cell = (r.transcript or ("(STT/ağ hatası)" if r.network_error else "(anlaşılamadı)")).replace("|", "/")
        lines.append(f"| {r.number} | {r.item.say} | {cell} | {expected} | {r.evaluation.detected} | "
                     f"{'✅' if r.evaluation.correct else '❌'} | {r.evaluation.reason or r.error} | {stt} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, csv_path, summary


# ====================================================================== oturum
class SegmentRecorder:
    """Sürekli açık tek mikrofon; istenildiğinde bir sonraki konuşma bölütünü verir."""

    def __init__(self, source: FrameSource, config: SegmenterConfig | None = None,
                 on_status: Callable[[str], None] | None = None):
        self._segments: queue.Queue[AudioSegment] = queue.Queue()
        self.capture = AudioCapture(source, on_segment=self._segments.put, on_status=on_status, config=config)

    def start(self, calibration_wait_s: float = 1.0) -> None:
        self.capture.start()
        self.capture.connected.wait(10)
        time.sleep(calibration_wait_s)  # ortam gürültüsü ölçülürken konuşmayın

    def next_segment(self, timeout_s: float = 12.0) -> AudioSegment | None:
        while not self._segments.empty():  # önceki maddeden kalan sesleri at
            self._segments.get_nowait()
        try:
            return self._segments.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def count_extra(self, wait_s: float = 0.6) -> int:
        """Son bölütten sonra kısa süre içinde gelen ek bölüt sayısı (atılır, birleştirilmez).

        Ek bölüt, söylenen cümlenin sessizlik nedeniyle ikiye bölündüğünü gösterir; test
        uygulamayla aynı davranışı ölçmek için yalnızca ilk bölüt değerlendirilir.
        """
        extra = 0
        deadline = time.monotonic() + wait_s
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining > 0:
                    self._segments.get(timeout=remaining)
                else:
                    self._segments.get_nowait()
            except queue.Empty:
                return extra
            extra += 1

    def stop(self) -> None:
        self.capture.stop()


def transcribe_with_retry(stt: SpeechToText, segment: AudioSegment, attempts: int = 3) -> tuple[SttResult | None, str]:
    for attempt in range(attempts):
        try:
            return stt.transcribe(segment), ""
        except SttNetworkError as exc:
            if attempt == attempts - 1:
                return None, f"ağ hatası: {exc}"
            time.sleep(2 * (attempt + 1))
    return None, ""


def run_live_test(source: FrameSource, stt: SpeechToText, parser: CommandParser, output_dir: Path,
                  save_audio: bool = False, items: list[VoiceTestItem] | None = None,
                  input_func: Callable[[str], str] = input, print_func: Callable[[str], None] = print,
                  note: str = "", config: SegmenterConfig | None = None,
                  slow_warning_s: float = 4.0) -> tuple[Path, Path, Summary] | None:
    items = items or load_test_set()
    audio_dir = output_dir / f"ses_{datetime.now():%Y%m%d_%H%M%S}"
    manifest: list[dict] = []
    recorder = SegmentRecorder(source, config=config, on_status=print_func)
    print_func("Mikrofon açılıyor; ortam gürültüsü ölçülürken 1 saniye sessiz kalın...")
    recorder.start()
    warning = bluetooth_warning(source.name)
    if warning:
        print_func(warning)
    print_func(f"Mikrofon: {source.name}. {len(items)} madde okunacak.\n"
               "Her maddede Enter'a basın, ardından metni normal sesle okuyun.\n"
               "Komutlar: Enter = kaydet, a = atla, q = testi bitir.\n")
    results: list[ItemResult] = []
    skipped = 0
    try:
        for number, item in enumerate(items, start=1):
            label = "KOMUT" if item.is_command else "NORMAL KONUŞMA (komut değil)"
            choice = input_func(f"[{number}/{len(items)}] {label}: «{item.say}»  > ").strip().lower()
            if choice == "q":
                skipped += len(items) - number + 1
                break
            if choice == "a":
                skipped += 1
                continue
            print_func("  Dinleniyor... konuşun.")
            segment = recorder.next_segment()
            if segment is None:
                print_func("  Ses algılanmadı (12 sn). Madde atlandı; mikrofon seviyesini --mic-check ile "
                           "kontrol edin.")
                skipped += 1
                continue
            result, error = transcribe_with_retry(stt, segment)
            transcript = choose_transcript(result, parser.is_addressed) if result else None
            evaluation = evaluate_result(item, transcript, parser, error)
            end_to_intent = (datetime.now() - (segment.voice_ended_at or segment.ended_at)).total_seconds()
            extra = recorder.count_extra()
            audio_file = ""
            if save_audio:
                audio_file = segment.save_wav(audio_dir / f"{number:02d}.wav").name
                manifest.append({"number": number, "file": audio_file})
            results.append(ItemResult(number, item, transcript, result.alternatives if result else [], evaluation,
                                      result.latency_s if result else None, end_to_intent, audio_file, error,
                                      extra_segments=extra))
            mark = "✅" if evaluation.correct else "❌"
            shown = transcript or ("(STT/ağ hatası)" if error else "(anlaşılamadı)")
            split = f"  [uyarı: cümle {extra + 1} parçaya bölündü]" if extra else ""
            print_func(f"  {mark} Tanınan: {shown}  → {evaluation.detected} "
                       f"{evaluation.reason}  [{end_to_intent:.2f} sn]{split}")
    finally:
        recorder.stop()
    if save_audio and manifest:
        (audio_dir / "manifest.json").write_text(json.dumps({"items": manifest}, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    if not results:
        print_func("Hiç kayıt yapılmadı; rapor oluşturulmadı.")
        return None
    context = {"machine": platform.node(), "device": source.name, "engine": getattr(stt, "name", "?"),
               "language": getattr(stt, "language", "tr-TR"), "source": "gerçek mikrofon", "note": note}
    return write_report(results, skipped, output_dir, context, parser, slow_warning_s)


def run_replay_test(audio_dir: Path, stt: SpeechToText, parser: CommandParser, output_dir: Path,
                    items: list[VoiceTestItem] | None = None) -> tuple[Path, Path, Summary] | None:
    """--save-audio ile saklanan kayıtları yeniden tanır (mikrofon gerekmez)."""
    items = items or load_test_set()
    manifest = json.loads((audio_dir / "manifest.json").read_text(encoding="utf-8"))["items"]
    results: list[ItemResult] = []
    for entry in manifest:
        number = int(entry["number"])
        item = items[number - 1]
        source = WavFileSource(audio_dir / entry["file"], tail_silence_ms=0)
        source.open()
        pcm = b""
        while True:
            try:
                pcm += source.read()
            except EOFError:
                break
        now = datetime.now()
        segment = AudioSegment(pcm=pcm, sample_rate=source.sample_rate, started_at=now, ended_at=now)
        result, error = transcribe_with_retry(stt, segment)
        transcript = choose_transcript(result, parser.is_addressed) if result else None
        results.append(ItemResult(number, item, transcript, result.alternatives if result else [],
                                  evaluate_result(item, transcript, parser, error),
                                  result.latency_s if result else None, None,
                                  entry["file"], error))
    if not results:
        return None
    context = {"machine": platform.node(), "device": f"kayıt klasörü: {audio_dir.name}",
               "engine": getattr(stt, "name", "?"), "language": getattr(stt, "language", "tr-TR"),
               "source": "kayıtlı ses dosyalarından yeniden değerlendirme"}
    return write_report(results, len(items) - len(results), output_dir, context, parser)


# ====================================================================== kurulum kontrolü
def run_mic_check(source: FrameSource, stt: SpeechToText | None, parser: CommandParser, speech,
                  print_func: Callable[[str], None] = print, phrase: str = "Jarvis, gündemi oku",
                  expected_intent: str = "READ_AGENDA", input_func: Callable[[str], str] = input,
                  config: SegmenterConfig | None = None, slow_warning_s: float = 4.0) -> int:
    """Mikrofon seviyesi, tek cümle STT ve Türkçe TTS denemesi. Sorun yoksa 0 döner.

    Başarı ölçütü (Faz 4 saha bulgusu üzerine sıkılaştırıldı): söylenen cümle tam olarak beklenen
    komuta (varsayılan READ_AGENDA) dönüşmelidir. UNKNOWN, kesik komut, yanlış komut, ağ hatası,
    Türkçe olmayan TTS sesi ve Bluetooth kulaklık mikrofonu ayrı ayrı sorun/uyarı olarak sayılır.
    """
    from .audio_capture import MicrophoneError, measure_levels  # noqa: PLC0415

    problems = 0
    warnings = 0
    print_func("1/3 Mikrofon seviyesi: 3 saniye SESSİZ kalın...")
    try:
        report = measure_levels(source, 3.0)
    except MicrophoneError as exc:
        print_func(f"   HATA: {exc}")
        return 1
    print_func(f"   Aygıt: {report.device} ({report.sample_rate} Hz)")
    print_func(f"   Gürültü tabanı: {report.noise_floor:.0f}   Önerilen eşik: {report.suggested_threshold:.0f}")
    if report.peak < 1:
        print_func("   HATA: Mikrofondan hiç ses gelmiyor. Windows Ayarlar > Gizlilik > Mikrofon iznini ve "
                   "sessize alma düğmesini kontrol edin.")
        problems += 1
    bt = bluetooth_warning(report.device)
    if bt:
        print_func(f"   UYARI: {bt}")
        warnings += 1

    print_func(f"2/3 Konuşma tanıma: Enter'a basın ve «{phrase}» deyin.")
    if stt is None:
        print_func("   HATA: STT motoru kullanılamıyor.")
        problems += 1
    else:
        input_func("")
        recorder = SegmentRecorder(source, config=config, on_status=print_func)
        recorder.start(calibration_wait_s=0.8)
        print_func("   Dinleniyor...")
        segment = recorder.next_segment(timeout_s=10)
        recorder.stop()
        if segment is None:
            print_func("   HATA: Konuşma algılanmadı. Daha yüksek sesle konuşun veya .env içinde VAD_THRESHOLD "
                       "değerini önerilen eşiğin biraz altına ayarlayın.")
            problems += 1
        else:
            result, error = transcribe_with_retry(stt, segment)
            if error:
                print_func(f"   HATA: {error} (internet bağlantısını kontrol edin)")
                problems += 1
            elif result is None:
                print_func(f"   HATA: Ses kaydedildi ({segment.duration:.1f} sn) ama anlaşılamadı. Mikrofona yaklaşın.")
                problems += 1
            else:
                text = choose_transcript(result, parser.is_addressed)
                intent = parser.parse(text)
                print_func(f"   Tanınan: «{text}» (STT {result.latency_s:.2f} sn, ses {segment.duration:.1f} sn)")
                if result.latency_s >= slow_warning_s:
                    print_func(f"   UYARI: Tanıma yavaş ({result.latency_s:.1f} sn ≥ {slow_warning_s:.1f} sn). "
                               "İnternet bağlantısını kontrol edin; sorun sürerse logs/jarvis.log içindeki "
                               "'STT ... YAVAŞ' satırlarını inceleyin.")
                    warnings += 1
                if intent is None:
                    print_func("   HATA: Komut algılanmadı (uyandırma kelimesi 'Jarvis' duyulmadı).")
                    problems += 1
                elif intent.type.value != expected_intent:
                    truncated = (detect_truncation(str(intent.params.get("command", "")))
                                 if intent.type.value == "UNKNOWN" else None)
                    detail = " — komutun sonu kesilmiş görünüyor (VAD_TAIL_KEEP_MS artırılabilir)" if truncated else ""
                    print_func(f"   HATA: Komut yanlış anlaşıldı: {intent.type.value} "
                               f"(beklenen {expected_intent}){detail}.")
                    problems += 1
                else:
                    print_func(f"   Komut: {intent.type.value} ✓")

    print_func("3/3 Sesli yanıt: bir cümle seslendirilecek.")
    print_func(f"   {speech.status_message}")
    job = speech.say("Merhaba, ben JARVIS. Türkçe sesim çalışıyor.")
    wait_idle = getattr(speech, "wait_idle", None)
    if wait_idle is not None:
        wait_idle(30)
    played = getattr(job, "status", "done")  # Pocket: gerçekten çalındı mı? (pyttsx3 None döndürür)
    ready = getattr(speech, "ready", None)
    if ready is not None and not ready.is_set():
        played = "model henüz yüklenmedi"
    elif ready is not None and job is None and speech.enabled:
        played = "seslendirilmedi"

    if not speech.enabled:
        print_func("   HATA: Sesli yanıt kapalı veya başlatılamadı.")
        problems += 1
    elif played != "done":
        print_func(f"   HATA: Ses çalınamadı ({getattr(job, 'error', '') or played}).")
        problems += 1
    elif not getattr(speech, "voice_name", None):
        print_func("   HATA: Türkçe TTS sesi bulunamadı; yanıtlar varsayılan (İngilizce) sesle okunuyor. "
                   "docs/WINDOWS_SES_KURULUMU.md 'Türkçe ses' bölümüne bakın.")
        problems += 1
    summary = "her şey çalışıyor." if problems == 0 else f"{problems} sorun bulundu (yukarıya bakın)."
    if warnings:
        summary += f" {warnings} uyarı var."
    print_func("Sonuç: " + summary)
    return 0 if problems == 0 else 1
