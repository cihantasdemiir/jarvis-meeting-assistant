"""J.A.R.V.I.S Toplantı Asistanı — giriş noktası.

Kullanım:
    python main.py              # mikrofon + sesli yanıt
    python main.py --text       # klavye modu
    python main.py --no-tts     # sesli yanıt kapalı
    python main.py --demo       # örnek toplantı (gerçek e-posta gönderilmez)
    python main.py --demo-live  # final video: gerçek mikrofonla temiz, izole canlı toplantı (e-posta gönderilmez)
    python main.py --list-mics  # mikrofonları listele
    python main.py --mic 2      # belirli mikrofonu kullan
    python main.py --participants data/ekip_a.json --agenda data/haftalik.json  # bu toplantı için liste/şablon

Ses kurulumu ve doğruluk ölçümü:
    python main.py --mic-check                # mikrofon seviyesi + tek cümle STT + sesli yanıt denemesi
    python main.py --voice-test --save-audio  # komut doğruluk testi (rapor: output/voice_tests/)
    python main.py --voice-test-replay KLASÖR # kayıtlı sesleri yeniden değerlendir

Türkçe sesli yanıt (Pocket TTS Turkish, .env TTS_ENGINE=pocket):
    python main.py --tts-bench               # model yükleme, ilk ses, gerçek zaman faktörü, RAM, komut gecikmesi
    python main.py --tts-bench --tts-play    # ayrıca ilk cümleyi hoparlörden çal
    python main.py --tts-download            # modeli bir kez indir (onay sorulur, ~440 MB, internet gerekir)

Tutanak işlemleri (program yeniden başlatıldıktan sonra da kullanılabilir):
    python main.py --list-meetings          # kayıtlı toplantılar
    python main.py --minutes [ID]           # PDF tutanağı (yeniden) oluştur; ID yoksa son toplantı
    python main.py --send-minutes [ID]      # tutanağı e-postayla gönder (zaten gidenlere tekrar gitmez)
    python main.py --send-minutes --force   # herkese yeniden gönder
    python main.py --export-tasks [ID|son|acik]  # görevleri Excel'e aktar (yalnızca okur)
    python main.py --smtp-check             # SMTP bağlantı + TLS + giriş denemesi (e-posta GÖNDERMEZ)
    python main.py --smtp-check --smtp-ssl  # aynı denemeyi 465 / doğrudan SSL ile yap

Yerel LLM (Ollama; varsayılan kapalı, .env LLM_ENABLED):
    python main.py --llm-check              # sunucu, yüklü modeller, GPU/CPU kullanımı, canlı/benchmark ayar farkı
    python main.py --trace-report           # girdi zinciri tanılaması: komutlar hangi aşamada anlaşılamadı (metinsiz)
    python main.py --llm-benchmark          # LLM_COMPARE_MODELS modellerini Türkçe test setiyle karşılaştır
    python main.py --analyze [ID]           # kayıtlı toplantıdan karar/görev TASLAKLARI ve özet taslağı
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config.settings import load_settings
from jarvis.app import build_assistant, configure_console, configure_logging, demo_live_settings, demo_settings
from jarvis.meeting.storage import MeetingRepository
from jarvis.reports.minutes_service import MinutesService
from jarvis.voice.listener import ScriptedListener, TextListener, list_microphones
from jarvis.voice.tts_factory import shutdown_speech
from jarvis.voice.voice_listener import build_voice_listener, segmenter_config_from

DEMO_SCRIPT = [
    # --- 1. toplantı: kayıt oluşturma
    "Jarvis, toplantıyı başlat: DEMO — Haftalık Üretim Toplantısı (1. hafta)",
    "Jarvis, katılımcılar: Kenan, Ayşe, Mehmet",
    "Jarvis, gündemi oku",
    "Jarvis, söz Ayşe'de",
    "Bu hafta iki hat birden bakımda olacak.",
    "Bakım takvimini tedarikçiyle netleştirmemiz gerekiyor.",
    "Jarvis, karar: Yeni üretim planı pazartesi uygulanacak",
    "Jarvis, görev: Mehmet raporu haftaya kadar hazırlasın",
    "Jarvis, görev: Ayşe bakım takvimini gelecek cumaya kadar paylaşsın",
    "Jarvis, söz Mehmet'te",
    "Kalite verilerini rapora ekleyeceğim.",
    "Jarvis, oylama: fazla mesai lehte 3 aleyhte 0",
    "Jarvis, otopark: ofis taşınması",
    "Jarvis, toplantıyı bitir",
    # --- 2. toplantı: önceki toplantının hatırlanması ve devreden görevler
    "Jarvis, toplantıyı başlat: DEMO — Haftalık Üretim Toplantısı (2. hafta)",
    "Jarvis, önceki toplantıyı özetle",
    "Jarvis, katılımcılar: Kenan, Ayşe, Mehmet",
    "Jarvis, söz Mehmet'te",
    "Raporu tamamladım ve paylaştım.",
    "Jarvis, görev tamamlandı: rapor",
    "Jarvis, açık görevleri oku",
    "Jarvis, kim konuşmadı",
    "Jarvis, süre durumu",
    "Jarvis, sonraki toplantı haftaya",
    "Jarvis, toplantıyı bitir",
    "Jarvis, tutanağı gönder",  # demo: gerçek gönderim yok, .eml taslağı yazılır
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S Toplantı Asistanı")
    parser.add_argument("--text", action="store_true", help="Klavye ile komut girişi")
    parser.add_argument("--participants", metavar="DOSYA",
                        help="Yeni toplantının aday katılımcı listesi (varsayılan .env PARTICIPANTS_FILE)")
    parser.add_argument("--agenda", metavar="DOSYA",
                        help="Yeni toplantının gündem dosyası (varsayılan .env AGENDA_FILE)")
    parser.add_argument("--no-tts", action="store_true", help="Sesli yanıtları kapat (yalnızca konsol)")
    parser.add_argument("--demo", action="store_true", help="Örnek toplantıyı çalıştır (e-posta gönderilmez)")
    parser.add_argument("--demo-live", action="store_true",
                        help="Final video için temiz, izole canlı toplantı: gerçek mikrofon; geçmiş/açık görev "
                             "devralınmaz; veriler data/demo_live ve output/demo_live altında; e-posta gönderilmez")
    parser.add_argument("--list-mics", action="store_true", help="Kullanılabilir mikrofonları listele")
    parser.add_argument("--list-meetings", action="store_true", help="Kayıtlı toplantıları listele")
    parser.add_argument("--minutes", nargs="?", const="son", metavar="ID",
                        help="Toplantının PDF tutanağını oluştur (ID verilmezse son toplantı)")
    parser.add_argument("--send-minutes", nargs="?", const="son", metavar="ID",
                        help="Tutanağı e-postayla gönder (ID verilmezse son kapatılan toplantı)")
    parser.add_argument("--force", action="store_true", help="--send-minutes ile: daha önce gönderilenlere de gönder")
    parser.add_argument("--mic", type=int, metavar="NO", help="Kullanılacak mikrofon numarası (--list-mics)")
    parser.add_argument("--stt", choices=["google", "whisper"], help="Konuşma tanıma motoru (varsayılan: .env)")
    parser.add_argument("--mic-check", action="store_true", help="Mikrofon, konuşma tanıma ve sesli yanıt kontrolü")
    parser.add_argument("--voice-test", action="store_true", help="Sesli komut doğruluk testini çalıştır")
    parser.add_argument("--save-audio", action="store_true", help="--voice-test ile: kayıtları WAV olarak sakla")
    parser.add_argument("--note", default="", help="--voice-test raporuna ortam notu (ör. 'klima açık, 1 m')")
    parser.add_argument("--voice-test-replay", metavar="KLASÖR", help="Saklanan test kayıtlarını yeniden değerlendir")
    # Yerel LLM (Ollama)
    parser.add_argument("--llm-check", action="store_true",
                        help="Ollama bağlantısı, yüklü modeller ve GPU/CPU kontrolü")
    parser.add_argument("--llm-benchmark", action="store_true", help="Yerel modelleri Türkçe test setiyle karşılaştır")
    parser.add_argument("--models", default="", help="--llm-benchmark ile: virgülle ayrılmış model listesi")
    parser.add_argument("--bench-kinds", default="router,extract,summary",
                        help="--llm-benchmark ile: router, extract, summary (virgülle)")
    parser.add_argument("--bench-limit", type=int, default=0, help="--llm-benchmark ile: her türden en fazla N senaryo")
    parser.add_argument("--bench-set", choices=["main", "holdout", "ek", "dogal"], default="main",
                        help="--llm-benchmark ile: main = 106 senaryo, holdout = 46 senaryo, "
                             "ek = Faz 5a.2 sorumlu/özet seti, dogal = Faz 5c.1 doğal dil seti (geliştirici kaynaklı)")
    parser.add_argument("--export-tasks", nargs="?", const="son", metavar="ID|son|acik",
                        help="Görevleri Excel'e aktar: toplantı kimliği, son toplantı (varsayılan) veya 'acik'")
    parser.add_argument("--smtp-check", action="store_true",
                        help="SMTP bağlantısı, TLS ve kimlik doğrulamayı dener; e-posta göndermez")
    parser.add_argument("--smtp-ssl", action="store_true",
                        help="--smtp-check ile: 465 portu ve doğrudan SSL (SMTP_USE_SSL=true) olarak dene")
    parser.add_argument("--trace-report", nargs="?", const="", metavar="DOSYA",
                        help="Girdi zinciri tanılama özeti (varsayılan: logs/pipeline_trace.jsonl)")
    parser.add_argument("--analyze", nargs="?", const="son", metavar="ID",
                        help="Kayıtlı toplantıyı analiz et: karar/görev taslakları ve özet taslağı (onaysız)")
    # Türkçe sesli yanıt (Faz 5b)
    parser.add_argument("--tts-bench", action="store_true",
                        help="Pocket TTS Turkish performans ölçümü (yükleme, ilk ses, RTF, RAM, komut gecikmesi)")
    parser.add_argument("--tts-runs", type=int, default=3, help="--tts-bench ile: her cümle için tekrar sayısı")
    parser.add_argument("--tts-device", choices=["cpu", "cuda"], help="--tts-bench ile: .env TTS_DEVICE yerine")
    parser.add_argument("--tts-play", action="store_true", help="--tts-bench ile: ilk cümleyi hoparlörden çal")
    parser.add_argument("--tts-download", nargs="?", const="", metavar="KLASÖR",
                        help="Türkçe ses modelini indir (onay sorulur). KLASÖR verilirse oraya; yoksa HF önbelleğine")
    return parser.parse_args(argv)


def run_list_mics() -> int:
    from jarvis.voice.audio_capture import list_input_devices

    devices, error = list_input_devices()
    if not error:
        if not devices:
            print("Hiç mikrofon bulunamadı.")
            return 1
        print("Kullanılabilir mikrofonlar (--mic NO veya .env MICROPHONE_INDEX için numarayı kullanın):")
        for index, name, rate in devices:
            print(f"  [{index}] {name} ({rate} Hz)")
        from jarvis.voice.audio_output import list_output_devices

        outputs = list_output_devices()
        if outputs:
            print("Hoparlörler (.env TTS_OUTPUT_DEVICE için):")
            for index, name in outputs:
                print(f"  [{index}] {name}")
        return 0
    names, error = list_microphones()
    if error:
        print(error)
        return 1
    if not names:
        print("Hiç mikrofon bulunamadı.")
        return 1
    print("Kullanılabilir mikrofonlar (MICROPHONE_INDEX için numarayı kullanın):")
    for index, name in enumerate(names):
        print(f"  [{index}] {name}")
    return 0


def run_list_meetings() -> int:
    settings = load_settings()
    repo = MeetingRepository(settings.db_path)
    meetings = repo.list_meetings()
    if not meetings:
        print("Kayıtlı toplantı yok.")
        return 0
    print("Kayıtlı toplantılar (en yeni üstte):")
    for row in meetings:
        meeting = repo.get(row["id"])
        email = (meeting.email_status.get("status") if meeting else None) or "gönderilmedi"
        print(f"  {row['id']}  {row['started_at'][:16].replace('T', ' ')}  [{row['status']}]  "
              f"{row['title']}  — e-posta: {email}")
    return 0


def run_minutes_command(meeting_ref: str, send: bool, force: bool) -> int:
    settings = load_settings()
    configure_logging(settings.logs_dir)
    settings.ensure_dirs()
    repo = MeetingRepository(settings.db_path)
    meeting = repo.get_last_ended() if meeting_ref == "son" else repo.get(meeting_ref)
    if meeting is None:
        print("Toplantı bulunamadı. Kayıtlı toplantılar için: python main.py --list-meetings")
        return 1
    service = MinutesService(repo, settings)
    result = service.send_minutes(meeting, force=force) if send else service.create_pdf(meeting)
    print(result.message)
    return 0 if result.ok else 1


def run_export_tasks(meeting_ref: str) -> int:
    """Görevleri Excel'e aktarır (yalnızca okur): ID / "son" toplantı veya "acik" (tüm açık görevler)."""
    settings = load_settings()
    configure_logging(settings.logs_dir)
    settings.ensure_dirs()
    repo = MeetingRepository(settings.db_path)
    service = MinutesService(repo, settings)
    if meeting_ref in ("acik", "açık"):
        result = service.export_tasks(repo.get_active(), scope="open")
    else:
        meeting = repo.get_last_ended() if meeting_ref == "son" else repo.get(meeting_ref)
        if meeting is None:
            print("Toplantı bulunamadı. Kayıtlı toplantılar için: python main.py --list-meetings")
            return 1
        result = service.export_tasks(meeting)
    print(result.message)
    return 0 if result.ok else 1


def _voice_tools(args, use_tts: bool):
    """Ses araçları için ortak kurulum: ayarlar, ayrıştırıcı, STT, mikrofon kaynağı, TTS."""
    from jarvis.voice.audio_capture import PyAudioSource
    from jarvis.voice.command_parser import CommandParser
    from jarvis.voice.stt import SttUnavailable, create_stt
    from jarvis.voice.tts_factory import create_speech_engine

    settings = load_settings()
    configure_logging(settings.logs_dir)
    settings.ensure_dirs()
    parser = CommandParser(settings.wake_word, settings.wake_word_aliases)
    stt, stt_error = None, None
    try:
        stt = create_stt(args.stt or settings.stt_engine, settings.stt_language, settings.stt_timeout_s,
                         settings.google_speech_key, resample_hz=settings.stt_resample_hz)
    except SttUnavailable as exc:
        stt_error = str(exc)
    source = PyAudioSource(device_index=args.mic if args.mic is not None else settings.microphone_index)
    speech = create_speech_engine(settings, use_tts, blocking=True, notify=print)
    return settings, parser, stt, stt_error, source, speech


def run_mic_check(args, use_tts: bool) -> int:
    from jarvis.voice.voice_test import run_mic_check as check

    settings, parser, stt, stt_error, source, speech = _voice_tools(args, use_tts)
    from jarvis.voice.tts_factory import shutdown_speech

    if stt_error:
        print(f"Konuşma tanıma kullanılamıyor: {stt_error}")
    try:
        return check(source, stt, parser, speech, config=segmenter_config_from(settings),
                     slow_warning_s=settings.stt_slow_warning_s)
    finally:
        shutdown_speech(speech, settings.tts_shutdown_timeout_s)


def run_voice_test(args) -> int:
    from jarvis.voice.voice_test import TARGET_ACCURACY, run_live_test, run_replay_test

    settings, parser, stt, stt_error, source, _speech = _voice_tools(args, use_tts=False)
    if stt_error:
        print(f"Konuşma tanıma kullanılamıyor: {stt_error}")
        return 1
    output_dir = settings.output_dir / "voice_tests"
    if args.voice_test_replay:
        outcome = run_replay_test(Path(args.voice_test_replay), stt, parser, output_dir)
    else:
        outcome = run_live_test(source, stt, parser, output_dir, save_audio=args.save_audio, note=args.note,
                                config=segmenter_config_from(settings),
                                slow_warning_s=settings.stt_slow_warning_s)
    if outcome is None:
        return 1
    md_path, csv_path, summary = outcome
    print(f"\nKomut doğruluğu: %{summary.command_accuracy:.1f} ({summary.correct_commands}/{summary.total_commands}) "
          f"— hedef ≥ %{TARGET_ACCURACY:.0f}")
    print(f"Yanlış tetikleme: {summary.false_triggers}/{summary.total_speech}   "
          f"En uzun konuşma sonu → niyet: {summary.max_end_to_intent:.2f} sn")
    print(f"Rapor: {md_path}\nTablo (Excel): {csv_path}")
    return 0


def run_llm_check() -> int:
    from jarvis.llm.client import LlmError, OllamaClient, check_privacy

    from jarvis.llm.services import config_report

    settings = load_settings()
    configure_logging(settings.logs_dir)
    print(f"LLM_ENABLED={settings.llm_enabled}  OLLAMA_HOST={settings.ollama_host}  LLM_MODEL={settings.llm_model}")
    print(f"LLM_ROUTER_ENABLED={settings.llm_router_enabled}  "
          f"LLM_COMPARE_MODELS={','.join(settings.llm_compare_models)}")
    for line in config_report(settings):
        print(line)
    try:
        check_privacy(settings.ollama_host, settings.llm_model, settings.llm_allow_remote)
        client = OllamaClient.from_settings(settings)
        print(f"Ollama sürümü: {client.version()}")
        models = client.list_models()
    except LlmError as exc:
        print(f"HATA: {exc}")
        return 1
    print("Yüklü modeller: " + (", ".join(models) if models else "yok"))
    problems = 0
    for model in dict.fromkeys([settings.llm_model, *settings.llm_compare_models]):
        if not client.has_model(model):
            print(f"  {model}: YÜKLÜ DEĞİL → ollama pull {model}")
            problems += 1
            continue
        try:
            result = client.chat_json(model, "Yalnızca JSON döndür.", "Türkçe 'tamam' kelimesini yaz.",
                                      {"type": "object", "properties": {"cevap": {"type": "string"}},
                                       "required": ["cevap"]}, timeout=max(120.0, settings.llm_timeout_s),
                                      num_predict=30)
        except LlmError as exc:
            print(f"  {model}: HATA {exc}")
            problems += 1
            continue
        running = [m for m in client.running_models() if m.name == model]
        processor = running[0].processor if running else "bellekte görünmüyor"
        print(f"  {model}: çalışıyor — {result.stats.wall_s:.1f} sn (yükleme {result.stats.load_s:.1f} sn), "
              f"{result.stats.tokens_per_s:.1f} token/sn, işlemci: {processor}")
    if not settings.llm_enabled:
        print("Not: LLM_ENABLED=false; uygulama LLM kullanmıyor. Etkinleştirmek için .env içinde LLM_ENABLED=true.")
    return 0 if problems == 0 else 1


def run_smtp_check(use_ssl_465: bool) -> int:
    """Yalnızca bağlantı ve kimlik doğrulama; MAIL FROM / RCPT / DATA gönderilmez, e-posta gitmez."""
    from jarvis.mail.mailer import Mailer, describe_settings

    settings = load_settings()
    configure_logging(settings.logs_dir)
    if use_ssl_465:
        settings = settings.with_overrides(smtp_port=465, smtp_use_ssl=True, smtp_use_tls=False)
    print("SMTP kontrolü — e-posta GÖNDERİLMEZ (yalnızca bağlantı, TLS ve giriş).")
    for line in describe_settings(settings):
        print(f"  {line}")
    result = Mailer(settings).check_connection()
    for step in result.steps:
        print(f"  [{'TAMAM' if step.ok else 'HATA '}] {step.stage}: {step.detail}")
    if result.ok:
        print("Sonuç: bağlantı ve kimlik doğrulama başarılı. Gerçek gönderim sunucunun iletiyi kabul etmesiyle ve "
              "alıcının gelen kutusunu kontrol etmesiyle ayrıca doğrulanmalıdır.")
        return 0
    print(f"Sonuç: BAŞARISIZ — {result.error}")
    return 1


def run_trace_report(target: str) -> int:
    from jarvis.diagnostics import TRACE_FILE, trace_report

    settings = load_settings()
    path = Path(target) if target else settings.logs_dir / TRACE_FILE
    print(trace_report(path))
    return 0


def run_llm_benchmark(args) -> int:
    import platform

    from jarvis.llm.benchmark import (
        BENCHMARK_PATH,
        EXTRA_PATH,
        HOLDOUT_PATH,
        NATURAL_PATH,
        load_benchmark,
        run_benchmark,
    )
    from jarvis.llm.client import LlmPolicyError, OllamaClient, check_privacy
    from jarvis.voice.command_parser import CommandParser

    settings = load_settings()
    configure_logging(settings.logs_dir)
    settings.ensure_dirs()
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or settings.llm_compare_models
    try:
        for model in models:
            check_privacy(settings.ollama_host, model, settings.llm_allow_remote)
    except LlmPolicyError as exc:
        print(f"HATA: {exc}")
        return 1
    parser = CommandParser(settings.wake_word, settings.wake_word_aliases)
    kinds = {k.strip() for k in args.bench_kinds.split(",") if k.strip()}
    context = {"machine": platform.node(), "cpu": platform.processor(), "host": settings.ollama_host,
               "temperature": str(settings.llm_temperature), "num_ctx": str(settings.llm_num_ctx),
               "think": str(settings.llm_think), "note": args.note}
    data = load_benchmark({"holdout": HOLDOUT_PATH, "ek": EXTRA_PATH, "dogal": NATURAL_PATH}.get(args.bench_set,
                                                                                               BENCHMARK_PATH))
    context["bench_set"] = args.bench_set
    _reports, (md, csv_path, json_path) = run_benchmark(
        models, lambda: OllamaClient.from_settings(settings), parser,
        settings.output_dir / "llm_benchmark" / args.bench_set,
        kinds=kinds, limit=args.bench_limit or None, timeout_s=settings.llm_timeout_s,
        router_timeout_s=max(settings.llm_router_timeout_s, 30.0), context=context, data=data)
    print(f"\nRapor: {md}\nTablo (Excel): {csv_path}\nJSON: {json_path}")
    return 0


def run_analyze(meeting_ref: str) -> int:
    from jarvis.llm.analyzer import candidates_to_drafts
    from jarvis.llm.services import build_llm_services
    from jarvis.meeting.manager import MeetingManager, describe_draft
    from jarvis.voice.command_parser import CommandParser

    settings = load_settings()
    configure_logging(settings.logs_dir)
    settings.ensure_dirs()
    repo = MeetingRepository(settings.db_path)
    meeting = repo.get_last_ended() if meeting_ref == "son" else repo.get(meeting_ref)
    if meeting is None:
        print("Toplantı bulunamadı. Kayıtlar için: python main.py --list-meetings")
        return 1
    services = build_llm_services(settings, CommandParser(settings.wake_word, settings.wake_word_aliases))
    print(services.status)
    print(f"Analiz ediliyor: {meeting.title} ({len(meeting.transcript)} transkript satırı)...")
    result = services.analyzer.analyze(meeting)
    drafts = candidates_to_drafts(result.candidates, meeting, 1)
    summary = {"method": result.method, "overall": result.overall_summary, "items": result.item_summaries,
               "warnings": result.warnings, "stats": dict(result.stats.__dict__)}
    stored = MeetingManager(repo, settings).store_analysis(meeting, drafts, summary)
    print(stored.message)
    for warning in result.warnings:
        print(f"UYARI: {warning}")
    for draft in meeting.ai_drafts:
        if draft.status == "taslak":
            print("  " + describe_draft(draft) + (f" [{'; '.join(draft.notes)}]" if draft.notes else ""))
    if result.overall_summary:
        print(f"\nÖzet taslağı ({result.method}, onaylanmamış):\n{result.overall_summary}")
    print("\nTaslaklar resmî kayıt değildir. Toplantı açıkken 'Jarvis, taslak N onayla' ile onaylanabilir.")
    return 0


def run_tts_download(target: str) -> int:
    """Modeli yalnızca kullanıcının açık onayıyla indirir (uygulama kendiliğinden indirmez)."""
    from jarvis.voice.pocket_backend import TtsUnavailable, download_model, resolve_model_folder

    settings = load_settings()
    folder = Path(target) if target else settings.tts_model_dir
    try:
        existing = resolve_model_folder(folder, settings.tts_model_repo)
        print(f"Model zaten bu bilgisayarda: {existing}")
        return 0
    except TtsUnavailable:
        pass
    where = str(folder) if folder else "Hugging Face önbelleği (%USERPROFILE%\\.cache\\huggingface)"
    print(f"İndirilecek: {settings.tts_model_repo} (yaklaşık 440 MB; lisans: model CC-BY-4.0)\nHedef: {where}")
    if input("İnternetten indirilsin mi? [e/H] ").strip().lower() not in {"e", "evet", "y", "yes"}:
        print("İptal edildi; hiçbir şey indirilmedi.")
        return 1
    try:
        path = download_model(settings.tts_model_repo, folder)
    except Exception as exc:
        print(f"HATA: İndirilemedi: {exc}")
        return 1
    print(f"İndirildi: {path}")
    if folder:
        print(f"Kullanmak için .env içinde: TTS_MODEL_DIR={folder}")
    return 0


def run_tts_bench(args) -> int:
    from jarvis.voice.tts_bench import run_tts_benchmark

    settings = load_settings()
    configure_logging(settings.logs_dir)
    settings.ensure_dirs()
    if args.tts_device:
        settings = settings.with_overrides(tts_device=args.tts_device)
    return run_tts_benchmark(settings, runs=max(1, args.tts_runs), play=args.tts_play, note=args.note)


def run_demo(use_tts: bool) -> int:
    settings = demo_settings(load_settings())
    # Demo her çalıştırmada temiz başlar; gerçek veritabanına dokunulmaz.
    for suffix in ("", "-wal", "-shm"):
        path = settings.db_path.with_name(settings.db_path.name + suffix)
        if path.exists():
            path.unlink()
    configure_logging(settings.logs_dir)
    assistant = build_assistant(settings, use_tts=use_tts, demo=True)
    print("=== DEMO MODU — örnek veriler kullanılıyor, e-posta gönderilmez ===")
    print(assistant.speech.status_message)
    try:
        assistant.run(ScriptedListener(DEMO_SCRIPT, echo=lambda line: print(f"Siz > {line}")))
    finally:
        shutdown_speech(assistant.speech, settings.tts_shutdown_timeout_s)
    print(f"Demo veritabanı: {settings.db_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv)
    if args.list_mics:
        return run_list_mics()
    if args.list_meetings:
        return run_list_meetings()
    if args.minutes:
        return run_minutes_command(args.minutes, send=False, force=False)
    if args.send_minutes:
        return run_minutes_command(args.send_minutes, send=True, force=args.force)
    if args.llm_check:
        return run_llm_check()
    if args.export_tasks:
        return run_export_tasks(args.export_tasks)
    if args.smtp_check:
        return run_smtp_check(args.smtp_ssl)
    if args.trace_report is not None:
        return run_trace_report(args.trace_report)
    if args.llm_benchmark:
        return run_llm_benchmark(args)
    if args.analyze:
        return run_analyze(args.analyze)
    if args.tts_download is not None:
        return run_tts_download(args.tts_download)
    if args.tts_bench:
        return run_tts_bench(args)
    use_tts = not args.no_tts
    if args.mic_check:
        return run_mic_check(args, use_tts)
    if args.voice_test or args.voice_test_replay:
        return run_voice_test(args)
    if args.demo:
        return run_demo(use_tts)

    settings = load_settings()
    if args.participants or args.agenda:
        # Yalnızca YENİ açılacak toplantı için okunur; kurtarılan yarım toplantının listesi değişmez.
        settings = settings.with_overrides(
            participants_file=Path(args.participants) if args.participants else settings.participants_file,
            agenda_file=Path(args.agenda) if args.agenda else settings.agenda_file)
    if args.demo_live:
        settings = demo_live_settings(settings)
    configure_logging(settings.logs_dir)
    assistant = build_assistant(settings, use_tts=use_tts, live_demo=args.demo_live)
    if args.demo_live:
        print("=== CANLI DEMO — temiz oturum: önceki toplantılar ve açık görevler kullanılmaz; "
              "e-posta gönderilmez (yalnızca .eml taslağı) ===")
        print(f"Demo verileri: {settings.db_path.parent}\nDemo çıktıları: {settings.output_dir}")
    print(f"J.A.R.V.I.S hazır. {assistant.speech.status_message}")
    source = None
    if not args.text:
        listener, error = build_voice_listener(settings, assistant.speech, assistant.parser,
                                               microphone_index=args.mic, stt_engine=args.stt)
        if error:
            print(f"UYARI: Sesli mod başlatılamadı: {error}\n"
                  "       Klavye moduyla devam ediliyor. Mikrofonu denemek için: python main.py --mic-check")
        else:
            source = listener
            print(f"Dinleniyor: {listener.device_name}. Komutlara 'Jarvis' ile başlayın; diğer konuşmalar "
                  "transkripte yazılır. Çıkmak için 'Jarvis, çık' veya Ctrl+C.")
    from jarvis.voice.keyboard import ConsoleLineReader, KeyWatcher, esc_supported

    use_esc = settings.keyboard_stop and esc_supported()
    watcher = None
    if source is None:
        print("Klavye modu: komutları 'Jarvis,' ile başlatın; diğer satırlar transkripte yazılır. "
              "Çıkmak için 'Jarvis, çık'." + (" Konuşurken Esc sesi keser." if use_esc else ""))
        # Windows'ta satır tek okuyucuyla okunur: Esc ayrı bir iş parçacığı input()'tan karakter çalmadan yakalanır
        source = TextListener(input_func=ConsoleLineReader(assistant.escape_pressed)) if use_esc else TextListener()
    elif use_esc:
        print("Asistan konuşurken Esc tuşu sesi keser; 'Jarvis, dur' da kullanılabilir. Ctrl+C programı kapatır.")
        watcher = KeyWatcher(assistant.escape_pressed)
        watcher.start()
    try:
        assistant.run(source)
    finally:
        if watcher is not None:
            watcher.stop()
        shutdown_speech(assistant.speech, settings.tts_shutdown_timeout_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
