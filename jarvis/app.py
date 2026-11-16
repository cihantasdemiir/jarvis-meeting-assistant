"""Uygulama bileşenlerini birbirine bağlayan fabrika fonksiyonları."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import BASE_DIR, Settings

from .assistant import JarvisAssistant
from .dialog import DialogManager
from .meeting.manager import MeetingManager
from .diagnostics import TRACE_FILE, PipelineTrace
from .llm.services import build_llm_services, config_report
from .meeting.storage import MeetingRepository
from .reports.minutes_service import MinutesService
from .voice.command_parser import CommandParser
from .voice.speech_engine import SpeechEngine
from .voice.tts_factory import create_speech_engine

log = logging.getLogger(__name__)

DEMO_DIR_NAME = "demo"
DEMO_LIVE_DIR_NAME = "demo_live"


def configure_console() -> None:
    """Windows konsolunda Türkçe karakterlerin doğru görünmesi için UTF-8 çıkış."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def configure_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    handler = RotatingFileHandler(logs_dir / "jarvis.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


def demo_settings(settings: Settings) -> Settings:
    """Demo verisini gerçek veriden ayırır: ayrı veritabanı, örnek dosyalar, e-posta kapalı."""
    demo_dir = settings.output_dir / DEMO_DIR_NAME
    return settings.with_overrides(
        db_path=demo_dir / "demo.db",
        output_dir=demo_dir,
        participants_file=BASE_DIR / "data" / "participants.example.json",
        agenda_file=BASE_DIR / "data" / "agenda.example.json",
        meeting_title="DEMO — Üretim Planlama Toplantısı",
        meeting_chair=settings.meeting_chair or "Kenan Çetin",
        meeting_location=settings.meeting_location or "Toplantı Odası 1",
        auto_send_minutes=False,
        mail_dry_run=True,
        mail_redirect_to="",
    )


def demo_live_settings(settings: Settings, now: datetime | None = None) -> Settings:
    """Final video için izole canlı demo (`--demo-live`).

    Gerçek mikrofon, STT, TTS, LLM, gündem ve katılımcı dosyaları normal çalışmadaki gibi kullanılır. Değişenler:
      * Her çalıştırma YENİ ve BOŞ bir oturum klasörü açar: data/demo_live/<zaman>/jarvis.db ve
        output/demo_live/<zaman>/. Bu yüzden normal veritabanındaki ya da önceki demo oturumlarındaki toplantılar,
        açık görevler ve yarım toplantılar okunmaz, kurtarılmaz, devralınmaz.
      * Normal veritabanına ve output/ altındaki normal dosyalara hiçbir şey yazılmaz.
      * E-posta hiçbir yoldan gerçek SMTP ile gönderilmez (otomatik gönderim kapalı; manuel gönderim deneme kipinde
        .eml taslağı yazar). .env'deki SMTP ayarları değiştirilmez.
    """
    stamp = f"{(now or datetime.now()):%Y%m%d-%H%M%S}"
    data_root = settings.data_dir / DEMO_LIVE_DIR_NAME
    output_root = settings.output_dir / DEMO_LIVE_DIR_NAME
    session, counter = stamp, 2
    while (data_root / session).exists() or (output_root / session).exists():
        session, counter = f"{stamp}_{counter}", counter + 1
    return settings.with_overrides(
        db_path=data_root / session / "jarvis.db",
        output_dir=output_root / session,
        auto_send_minutes=False,
        mail_dry_run=True,
        mail_redirect_to="",
    )


def build_assistant(settings: Settings, use_tts: bool, demo: bool = False,
                    speech: SpeechEngine | None = None, live_demo: bool = False) -> JarvisAssistant:
    """`live_demo=True` (--demo-live): canlı çalışma aynıdır; yalnızca tutanak servisi deneme kipindedir (gerçek
    SMTP yok). Toplantılar örnek veri olarak işaretlenmez; izolasyonu ayrı oturum veritabanı sağlar."""
    settings.ensure_dirs()
    repository = MeetingRepository(settings.db_path)
    manager = MeetingManager(repository, settings, demo=demo)
    parser = CommandParser(settings.wake_word, settings.wake_word_aliases,
                           near_misses=settings.wake_word_near_misses)
    for warning in settings.config_warnings:
        print(f"UYARI (.env): {warning}", flush=True)
    # Demo, senaryoyu sesle eş zamanlı anlatsın diye beklemeli kipte çalışır; canlı kipte yanıt beklemez.
    engine = speech or create_speech_engine(settings, use_tts, blocking=demo,
                                            notify=lambda message: print(message, flush=True))
    minutes = MinutesService(repository, settings, demo=demo or live_demo)
    llm = build_llm_services(settings, parser)
    # LLM durumu her açılışta gösterilir (canlı testte kapalı olduğu fark edilmemişti)
    for line in config_report(settings):
        print(line, flush=True)
    if settings.llm_enabled:
        print(llm.status, flush=True)
    log.info("LLM yapılandırması: %s", " | ".join(config_report(settings)))
    dialog = DialogManager(timeout_s=settings.dialog_timeout_s,
                           unaddressed_window_s=settings.dialog_unaddressed_window_s, enabled=settings.dialog_enabled)
    trace = PipelineTrace(settings.logs_dir / TRACE_FILE if settings.pipeline_trace else None,
                          include_text=settings.pipeline_trace_text, llm_enabled=llm.router is not None,
                          llm_status=llm.status)
    return JarvisAssistant(manager, parser, engine, require_wake=settings.text_mode_require_wake, minutes=minutes,
                           llm=llm, dialog=dialog, propose_unaddressed=settings.unaddressed_command_prompt,
                           trace=trace)
