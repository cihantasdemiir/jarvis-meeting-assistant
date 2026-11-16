"""Uygulama ayarları.

Tüm ayarlar `.env` dosyasından veya ortam değişkenlerinden okunur. Şifre ve
API anahtarı gibi gizli bilgiler asla koda yazılmaz.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

_TRUE_VALUES = {"1", "true", "evet", "yes", "on"}


def _load_dotenv(env_file: Path | None) -> str:
    """python-dotenv kuruluysa .env dosyasını ortam değişkenlerine yükler.

    JARVIS_ENV_FILE ortam değişkeni başka bir dosya gösterebilir; "none" değeri .env okumayı kapatır
    (testlerin gerçek veritabanına veya SMTP ayarlarına dokunmaması için).
    """
    override = os.environ.get("JARVIS_ENV_FILE", "").strip()
    if override.lower() == "none":
        return "okunmadı (JARVIS_ENV_FILE=none)"
    path = env_file or (Path(override) if override else BASE_DIR / ".env")
    if not path.exists():
        return f"bulunamadı ({path}); varsayılan ayarlar kullanılıyor"
    try:
        from dotenv import load_dotenv  # noqa: PLC0415
    except ImportError:
        return f"OKUNAMADI ({path}): python-dotenv kurulu değil; 'pip install -r requirements.txt' çalıştırın"
    # Mevcut ortam değişkenleri .env'deki değerlerden önceliklidir.
    load_dotenv(path, override=False)
    return f"okundu ({path})"


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    """Uygulamanın çalışma ayarları (değiştirilemez)."""

    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    output_dir: Path = BASE_DIR / "output"
    logs_dir: Path = BASE_DIR / "logs"
    db_path: Path = BASE_DIR / "data" / "jarvis.db"
    participants_file: Path = BASE_DIR / "data" / "participants.json"
    agenda_file: Path = BASE_DIR / "data" / "agenda.json"

    # Toplantı varsayılanları
    meeting_title: str = "Haftalık Değerlendirme Toplantısı"
    meeting_chair: str = ""
    meeting_location: str = ""
    company_name: str = ""
    logo_path: Path | None = None

    # Ses
    wake_word: str = "jarvis"
    wake_word_aliases: list[str] = field(
        default_factory=lambda: ["jarvıs", "carvis", "cervis", "çarvis", "jarves", "jarviz", "javis", "carviz",
                                 "çarviz", "jarviş"]
    )
    # Yakın hitaplar (ör. "charles"): HİTAP SAYILMAZ, komut çalıştırmaz; yalnızca "şunu mu demek istediniz?"
    # onayı üretir. Varsayılan boş (kapalı) — gerçek katılımcı adlarıyla çakışma riski nedeniyle (Faz 5a.3).
    wake_word_near_misses: list[str] = field(default_factory=list)
    text_mode_require_wake: bool = True
    stt_engine: str = "google"
    stt_language: str = "tr-TR"
    microphone_index: int | None = None
    # Sesli yanıt motoru (Faz 5b): "pocket" = Pocket TTS Turkish (varsayılan), "pyttsx3" = eski Windows SAPI5
    # motoru (yalnızca açıkça seçilirse), "none" = yalnızca konsol. Pocket yüklenemezse İngilizce sese GEÇİLMEZ.
    tts_engine: str = "pocket"
    tts_rate: int = 165  # yalnızca pyttsx3
    tts_volume: float = 1.0
    tts_voice_hint: str = "tolga"  # yalnızca pyttsx3
    # Asistan konuştuktan sonra mikrofonun sesi yok sayacağı süre (hoparlör yankısı için)
    tts_echo_guard_ms: int = 700
    # Pocket TTS Turkish
    tts_voice: str = "male_1"
    tts_device: str = "cpu"  # cpu | cuda (GPU yalnızca ölçümden sonra; Qwen ile VRAM paylaşılır)
    tts_model_dir: Path | None = None  # boşsa Hugging Face önbelleğinde YALNIZCA yerel arama yapılır
    tts_model_repo: str = "wite-tech/pocket-tts-turkish"
    tts_cpu_threads: int = 0  # 0 = PyTorch varsayılanı
    tts_load_timeout_s: float = 120.0
    tts_prebuffer_ms: int = 200  # oynatma başlamadan önce biriktirilen ses (takılmayı önler)
    tts_max_queue: int = 5  # bekleyen yanıt sınırı; aşılırsa en eski bekleyen (henüz başlamamış) yanıt atlanır
    tts_max_spoken_chars: int = 500  # daha uzun yanıtların yalnızca başı okunur; tamamı konsolda
    tts_output_device: int | None = None  # boşsa Windows varsayılan hoparlörü
    tts_shutdown_timeout_s: float = 15.0  # kapanışta son yanıtın bitmesi için en fazla bekleme
    # .env'deki geçersiz değerler için uyarılar (açılışta gösterilir)
    config_warnings: list[str] = field(default_factory=list)

    # Konuşma tanıma ayrıntıları
    stt_timeout_s: float = 8.0
    google_speech_key: str = ""
    # Konuşma algılama (VAD): 0 = ortam gürültüsüne göre otomatik eşik
    vad_threshold: int = 0
    vad_silence_ms: int = 800
    vad_min_speech_ms: int = 300
    vad_max_segment_s: float = 15.0
    # Bölüt sonunda korunan sessizlik (kelime sonu kesilmesin; Türkçe ekler düşük enerjilidir)
    vad_tail_keep_ms: int = 500
    # Konuşma sürerken eşiğin bu oranı da konuşma sayılır (bitiş histerezisi, yüzde)
    vad_release_percent: int = 50
    # STT'ye gönderilmeden önce ses bu hıza indirilir (48/44.1 kHz mikrofonlarda yükleme küçülür); 0 = kapalı
    stt_resample_hz: int = 16000
    # Bu süreyi aşan tanıma işlemleri günlüğe ve test raporuna "yavaş" olarak yazılır
    stt_slow_warning_s: float = 4.0
    # İnternet kesintisinde tanınamayan sesleri output/ses_bekleyen altına yedekle
    save_pending_audio: bool = True

    # Gündem
    default_item_minutes: int = 10
    timebox_warning_percent: int = 80

    # E-posta
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    auto_send_minutes: bool = False
    smtp_timeout: int = 30
    # Alıcılar: "all" = listedeki herkes, "present" = yalnızca toplantıya katılanlar
    mail_recipients_mode: str = "all"
    # Boş değilse yalnızca bu alan adlarındaki adreslere gönderilir (yanlış kişiye gönderimi önler)
    mail_allowed_domains: list[str] = field(default_factory=list)
    # Test için: dolu ise tüm e-postalar yalnızca bu adrese gider (konu satırına [TEST] eklenir)
    mail_redirect_to: str = ""
    # True ise SMTP kullanılmaz; e-posta output/outbox altına .eml olarak yazılır
    mail_dry_run: bool = False
    # Faz 6: takvim daveti (.ics). Süre söylenmediğinde kullanılan varsayılan; davet varsa tutanak e-postasına eklenir.
    ics_duration_min: int = 60
    mail_attach_ics: bool = True

    # PDF tutanak
    pdf_primary_color: str = "#1F3A5F"
    pdf_accent_color: str = "#197E89"  # PDF ince vurgu rengi (kurumsal prototip; e-postada kullanılmaz)
    pdf_font_regular: Path | None = None
    pdf_font_bold: Path | None = None

    # LLM (isteğe bağlı)
    openai_api_key: str = ""

    # Yerel LLM (Ollama). Varsayılan kapalı; açılsa bile yalnızca bu bilgisayardaki sunucu kullanılır.
    llm_enabled: bool = False
    ollama_host: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:4b"
    llm_compare_models: list[str] = field(default_factory=lambda: ["qwen3.5:4b", "qwen3:4b"])
    # Komut yorumlama etkileşimlidir: kısa zaman aşımı. Toplantı analizi uzun sürebilir.
    llm_router_timeout_s: float = 12.0
    llm_timeout_s: float = 180.0
    llm_num_ctx: int = 8192
    llm_temperature: float = 0.0
    llm_think: bool = False
    llm_keep_alive: str = "15m"
    # Uzak (başka bilgisayardaki) Ollama sunucusu ve "-cloud" modeller açıkça izin verilmedikçe reddedilir:
    # toplantı metni bilgisayarın dışına çıkmamalıdır (KVKK).
    llm_allow_remote: bool = False
    llm_router_enabled: bool = True
    llm_analysis_enabled: bool = True
    # LLM'nin önerdiği komut için "Jarvis, evet" onayının geçerlilik süresi
    llm_confirm_timeout_s: int = 60
    # Faz 5c.1: modelin bu güvenin altındaki önerileri işlem önerisine dönüştürülmez (yalnızca açıklama istenir)
    llm_min_confidence: float = 0.5
    # Faz 5c.2: çok turlu diyalog (belirsiz kişi, eksik görev, yarım katılım). Yanıtlar yine onaya gider.
    dialog_enabled: bool = True
    dialog_timeout_s: float = 60.0  # Jarvis'in sorusunun geçerlilik süresi
    dialog_unaddressed_window_s: float = 20.0  # "Jarvis" denmeden verilen kısa yanıtın kabul süresi
    # Faz 5c.2 canlı düzeltme: "Jarvis" denmeden söylenen komut kalıbı için onay önerisi (kapalıysa yalnızca işaret)
    unaddressed_command_prompt: bool = True
    # Girdi zinciri tanılaması (logs/pipeline_trace.jsonl). Metin varsayılan olarak YAZILMAZ.
    # Faz 5c.2 sesli kesme: asistan konuşurken yalnızca "Jarvis, dur/sus/yeter/dinle" dinlenir (barge_in.py)
    barge_in_enabled: bool = True
    barge_in_echo_margin: float = 2.5  # kullanıcı sesi yankı düzeyinin en az bu katı olmalı
    keyboard_stop: bool = True  # Esc: çalan sesli yanıtı keser (Windows konsolu)
    pipeline_trace: bool = True
    pipeline_trace_text: bool = False
    env_status: str = ""  # .env dosyasının okunup okunmadığı (değerler değil)
    # Uzun toplantılar bu büyüklükte parçalara bölünerek analiz edilir (karakter)
    llm_chunk_chars: int = 6000

    def with_overrides(self, **overrides: Any) -> "Settings":
        """Testler ve demo modu için ayarların değiştirilmiş kopyasını döndürür."""
        return replace(self, **overrides)

    def ensure_dirs(self) -> None:
        """Çalışma klasörlerini oluşturur."""
        for directory in (self.data_dir, self.output_dir, self.logs_dir, self.db_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


def load_settings(env_file: Path | None = None) -> Settings:
    """`.env` ve ortam değişkenlerinden ayarları yükler."""
    env_status = _load_dotenv(env_file)
    defaults = Settings()
    data_dir = _env_path("DATA_DIR", defaults.data_dir)
    logo_raw = _env_str("LOGO_PATH")
    mic_raw = _env_str("MICROPHONE_INDEX")
    warnings: list[str] = []
    tts_engine = _choice("TTS_ENGINE", defaults.tts_engine, TTS_ENGINES, warnings)
    tts_device = _choice("TTS_DEVICE", defaults.tts_device, TTS_DEVICES, warnings)
    tts_model_raw = _env_str("TTS_MODEL_DIR")
    tts_out_raw = _env_str("TTS_OUTPUT_DEVICE")
    return Settings(
        data_dir=data_dir,
        output_dir=_env_path("OUTPUT_DIR", defaults.output_dir),
        logs_dir=_env_path("LOGS_DIR", defaults.logs_dir),
        db_path=_env_path("DB_PATH", data_dir / "jarvis.db"),
        participants_file=_env_path("PARTICIPANTS_FILE", data_dir / "participants.json"),
        agenda_file=_env_path("AGENDA_FILE", data_dir / "agenda.json"),
        meeting_title=_env_str("MEETING_TITLE", defaults.meeting_title),
        meeting_chair=_env_str("MEETING_CHAIR"),
        meeting_location=_env_str("MEETING_LOCATION"),
        company_name=_env_str("COMPANY_NAME"),
        logo_path=_env_path("LOGO_PATH", BASE_DIR) if logo_raw else None,
        wake_word=_env_str("WAKE_WORD", defaults.wake_word).lower(),
        wake_word_aliases=_env_list("WAKE_WORD_ALIASES", defaults.wake_word_aliases),
        wake_word_near_misses=_env_list("WAKE_WORD_NEAR_MISSES", []),
        text_mode_require_wake=_env_bool("TEXT_MODE_REQUIRE_WAKE", True),
        stt_engine=_env_str("STT_ENGINE", defaults.stt_engine).lower(),
        stt_language=_env_str("STT_LANGUAGE", defaults.stt_language),
        microphone_index=int(mic_raw) if mic_raw.isdigit() else None,
        tts_engine=tts_engine,
        tts_rate=_env_int("TTS_RATE", defaults.tts_rate),
        tts_volume=max(0.0, min(1.0, _env_int("TTS_VOLUME_PERCENT", 100) / 100)),
        tts_voice_hint=_env_str("TTS_VOICE_HINT", defaults.tts_voice_hint).lower(),
        tts_echo_guard_ms=min(5000, max(0, _env_int("TTS_ECHO_GUARD_MS", defaults.tts_echo_guard_ms))),
        tts_voice=_env_str("TTS_VOICE", defaults.tts_voice) or defaults.tts_voice,
        tts_device=tts_device,
        tts_model_dir=_env_path("TTS_MODEL_DIR", BASE_DIR) if tts_model_raw else None,
        tts_model_repo=_env_str("TTS_MODEL_REPO", defaults.tts_model_repo) or defaults.tts_model_repo,
        tts_cpu_threads=min(64, max(0, _env_int("TTS_CPU_THREADS", 0))),
        tts_load_timeout_s=_env_float("TTS_LOAD_TIMEOUT_S", defaults.tts_load_timeout_s, 5.0, 900.0),
        tts_prebuffer_ms=min(2000, max(0, _env_int("TTS_PREBUFFER_MS", defaults.tts_prebuffer_ms))),
        tts_max_queue=min(50, max(1, _env_int("TTS_MAX_QUEUE", defaults.tts_max_queue))),
        tts_max_spoken_chars=min(5000, max(80, _env_int("TTS_MAX_SPOKEN_CHARS", defaults.tts_max_spoken_chars))),
        tts_output_device=_optional_index("TTS_OUTPUT_DEVICE", tts_out_raw, warnings),
        tts_shutdown_timeout_s=_env_float("TTS_SHUTDOWN_TIMEOUT_S", defaults.tts_shutdown_timeout_s, 0.0, 120.0),
        config_warnings=warnings,
        stt_timeout_s=float(max(2, _env_int("STT_TIMEOUT_S", 8))),
        google_speech_key=os.environ.get("GOOGLE_SPEECH_API_KEY", "").strip(),
        vad_threshold=max(0, _env_int("VAD_THRESHOLD", 0)),
        vad_silence_ms=min(3000, max(300, _env_int("VAD_SILENCE_MS", defaults.vad_silence_ms))),
        vad_min_speech_ms=max(100, _env_int("VAD_MIN_SPEECH_MS", defaults.vad_min_speech_ms)),
        vad_max_segment_s=float(min(55, max(5, _env_int("VAD_MAX_SEGMENT_S", 15)))),
        vad_tail_keep_ms=min(2000, max(100, _env_int("VAD_TAIL_KEEP_MS", defaults.vad_tail_keep_ms))),
        vad_release_percent=min(100, max(20, _env_int("VAD_RELEASE_PERCENT", defaults.vad_release_percent))),
        stt_resample_hz=_resample_rate(_env_int("STT_RESAMPLE_HZ", defaults.stt_resample_hz)),
        stt_slow_warning_s=_env_float("STT_SLOW_WARNING_S", defaults.stt_slow_warning_s, 0.5, 60.0),
        save_pending_audio=_env_bool("SAVE_PENDING_AUDIO", True),
        default_item_minutes=max(1, _env_int("DEFAULT_ITEM_MINUTES", defaults.default_item_minutes)),
        timebox_warning_percent=min(99, max(1, _env_int("TIMEBOX_WARNING_PERCENT", 80))),
        smtp_host=_env_str("SMTP_HOST"),
        smtp_port=_env_int("SMTP_PORT", 587),
        smtp_user=_env_str("SMTP_USER"),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=_env_str("SMTP_FROM") or _env_str("SMTP_USER"),
        smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
        smtp_use_ssl=_env_bool("SMTP_USE_SSL", False),
        auto_send_minutes=_env_bool("AUTO_SEND_MINUTES", False),
        ics_duration_min=min(480, max(15, _env_int("ICS_DURATION_MINUTES", 60))),
        mail_attach_ics=_env_bool("MAIL_ATTACH_ICS", True),
        smtp_timeout=max(5, _env_int("SMTP_TIMEOUT", 30)),
        mail_recipients_mode="present" if _env_str("MAIL_RECIPIENTS", "all").lower() == "present" else "all",
        mail_allowed_domains=[d.lower().lstrip("@") for d in _env_list("MAIL_ALLOWED_DOMAINS", [])],
        mail_redirect_to=_env_str("MAIL_REDIRECT_TO"),
        mail_dry_run=_env_bool("MAIL_DRY_RUN", False),
        pdf_primary_color=_env_str("PDF_PRIMARY_COLOR", defaults.pdf_primary_color),
        pdf_accent_color=_env_str("PDF_ACCENT_COLOR", defaults.pdf_accent_color),
        pdf_font_regular=_env_path("PDF_FONT_REGULAR", BASE_DIR) if _env_str("PDF_FONT_REGULAR") else None,
        pdf_font_bold=_env_path("PDF_FONT_BOLD", BASE_DIR) if _env_str("PDF_FONT_BOLD") else None,
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        llm_enabled=_env_bool("LLM_ENABLED", False),
        ollama_host=_env_str("OLLAMA_HOST", defaults.ollama_host).rstrip("/") or defaults.ollama_host,
        llm_model=_env_str("LLM_MODEL", defaults.llm_model),
        llm_compare_models=_env_list("LLM_COMPARE_MODELS", defaults.llm_compare_models),
        llm_router_timeout_s=_env_float("LLM_ROUTER_TIMEOUT_S", defaults.llm_router_timeout_s, 2.0, 120.0),
        llm_timeout_s=_env_float("LLM_TIMEOUT_S", defaults.llm_timeout_s, 5.0, 1800.0),
        llm_num_ctx=min(131072, max(2048, _env_int("LLM_NUM_CTX", defaults.llm_num_ctx))),
        llm_temperature=_env_float("LLM_TEMPERATURE", defaults.llm_temperature, 0.0, 2.0),
        llm_think=_env_bool("LLM_THINK", False),
        llm_keep_alive=_env_str("LLM_KEEP_ALIVE", defaults.llm_keep_alive),
        llm_allow_remote=_env_bool("LLM_ALLOW_REMOTE", False),
        llm_router_enabled=_env_bool("LLM_ROUTER_ENABLED", True),
        llm_analysis_enabled=_env_bool("LLM_ANALYSIS_ENABLED", True),
        llm_confirm_timeout_s=min(600, max(10, _env_int("LLM_CONFIRM_TIMEOUT_S", defaults.llm_confirm_timeout_s))),
        llm_min_confidence=_env_float("LLM_MIN_CONFIDENCE", defaults.llm_min_confidence, 0.0, 1.0),
        llm_chunk_chars=min(60000, max(1500, _env_int("LLM_CHUNK_CHARS", defaults.llm_chunk_chars))),
        dialog_enabled=_env_bool("DIALOG_ENABLED", True),
        dialog_timeout_s=_env_float("DIALOG_TIMEOUT_S", defaults.dialog_timeout_s, 10.0, 600.0),
        unaddressed_command_prompt=_env_bool("UNADDRESSED_COMMAND_PROMPT", True),
        barge_in_enabled=_env_bool("BARGE_IN_ENABLED", True),
        barge_in_echo_margin=_env_float("BARGE_IN_ECHO_MARGIN", defaults.barge_in_echo_margin, 1.2, 10.0),
        keyboard_stop=_env_bool("KEYBOARD_STOP", True),
        pipeline_trace=_env_bool("PIPELINE_TRACE", True),
        pipeline_trace_text=_env_bool("PIPELINE_TRACE_TEXT", False),
        env_status=env_status,
        dialog_unaddressed_window_s=_env_float("DIALOG_UNADDRESSED_WINDOW_S", defaults.dialog_unaddressed_window_s,
                                               0.0, 120.0),
    )


TTS_ENGINES = ("pocket", "pyttsx3", "none")
TTS_DEVICES = ("cpu", "cuda")


def _choice(name: str, default: str, allowed: tuple[str, ...], warnings: list[str]) -> str:
    """Seçenekli ayar: geçersiz değerde varsayılan kullanılır ve açılışta uyarı gösterilir."""
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw not in allowed:
        warnings.append(f"{name}={raw!r} geçersiz (geçerli: {', '.join(allowed)}); {default!r} kullanılıyor.")
        return default
    return raw


def _optional_index(name: str, raw: str, warnings: list[str]) -> int | None:
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        warnings.append(f"{name}={raw!r} sayı değil; varsayılan ses aygıtı kullanılıyor.")
        return None


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(high, max(low, value))


def _resample_rate(value: int) -> int:
    """0 = yeniden örnekleme kapalı; aksi halde 8-48 kHz arası."""
    if value <= 0:
        return 0
    return min(48000, max(8000, value))
