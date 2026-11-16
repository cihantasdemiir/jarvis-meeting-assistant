"""`python main.py --demo-live`: final video için izole canlı demo.

Normal veritabanı ve çıktılar korunur; demo her çalıştırmada boş bir oturum veritabanıyla başlar; e-posta gerçek
SMTP ile gönderilmez. Gerçek mikrofon/STT/TTS/Qwen kullanılmaz (aynı JarvisAssistant akışı klavye girdisiyle).
"""
from __future__ import annotations

import email
import hashlib
import io
import json
import smtplib
from datetime import date, datetime, timedelta
from email import policy
from pathlib import Path

import pytest

from jarvis.app import build_assistant, demo_live_settings
from jarvis.meeting.manager import MeetingManager
from jarvis.voice.speech_engine import SpeechEngine
from tests.conftest import FakeClock

AGENDA = [
    {"title": "Haftalık üretim planı ve hedefler", "planned_minutes": 5, "presenter": "Ayşe Yılmaz"},
    {"title": "Kalite kontrol ve uygunsuzluklar", "planned_minutes": 5, "presenter": "Mehmet Öztürk"},
    {"title": "Makine bakım ve arıza durumları", "planned_minutes": 5, "presenter": "Ahmet Yılmaz"},
    {"title": "Açık görevler ve gelecek hafta planı", "planned_minutes": 5},
]
PEOPLE = [
    {"name": "Kenan Çetin", "email": "kenan@example.com", "role": "Toplantı Başkanı"},
    {"name": "Ayşe Yılmaz", "email": "ayse@example.com"},
    {"name": "Mehmet Öztürk", "email": "mehmet.ozturk@example.com"},
    {"name": "Mehmet Yılmaz", "email": "mehmet.yilmaz@example.com"},
    {"name": "Ahmet Yılmaz", "email": "ahmet@example.com"},
]
T0 = datetime(2026, 10, 17, 15, 0, 0)


def tree_fingerprint(root: Path, skip: str = "demo_live") -> dict[str, str]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and skip not in p.parts}


@pytest.fixture
def normal(settings, repo):
    """Normal ortam: biten toplantıda açık görev + yarım kalmış toplantı + normal çıktı dosyası."""
    Path(settings.agenda_file).write_text(json.dumps(AGENDA, ensure_ascii=False), encoding="utf-8")
    Path(settings.participants_file).write_text(json.dumps(PEOPLE, ensure_ascii=False), encoding="utf-8")
    clock = FakeClock(datetime(2026, 10, 10, 10, 0, 0))
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting("Eski test toplantısı")
    manager.add_action("Mehmet Öztürk eski raporu hazırlasın")
    manager.add_decision("Eski karar")
    manager.end_meeting()
    clock.advance(days=1)
    manager.start_meeting("Yarım kalan normal toplantı")  # kapatılmadı
    manager.add_action("Ayşe yarım toplantı görevini yapsın")
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    (settings.output_dir / "normal_tutanak.pdf").write_bytes(b"%PDF-normal")
    return settings.with_overrides(smtp_host="smtp.example.com", smtp_from="jarvis@example.com",
                                   auto_send_minutes=True, mail_dry_run=False)


def make_demo(normal_settings, now: datetime = T0, logo: Path | None = None):
    demo = demo_live_settings(normal_settings, now=now)
    if logo is not None:
        demo = demo.with_overrides(logo_path=logo, company_name="Örnek Demir Çelik San. ve Tic. A.Ş.")
    a = build_assistant(demo, use_tts=False, speech=SpeechEngine(enabled=False, output=io.StringIO()),
                        live_demo=True)
    clock = FakeClock(now)
    a.manager.clock = clock
    a.minutes.clock = clock
    return demo, a, clock


def no_smtp(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("demo modunda gerçek SMTP bağlantısı açıldı")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)


# ====================================================================== izolasyon
def test_paths_are_isolated_per_session(normal):
    first = demo_live_settings(normal, now=T0)
    assert first.db_path == normal.data_dir / "demo_live" / "20261017-150000" / "jarvis.db"
    assert first.output_dir == normal.output_dir / "demo_live" / "20261017-150000"
    first.ensure_dirs()
    second = demo_live_settings(normal, now=T0)  # aynı saniye: yeni klasör
    assert second.db_path.parent.name == "20261017-150000_2"
    assert first.participants_file == normal.participants_file and first.agenda_file == normal.agenda_file
    assert first.auto_send_minutes is False and first.mail_dry_run is True and first.mail_redirect_to == ""
    assert normal.smtp_host == "smtp.example.com"  # normal ayarlar değişmez


def test_demo_does_not_read_history_or_recover(normal, settings):
    db_before = hashlib.sha256(settings.db_path.read_bytes()).hexdigest()
    _, a, _ = make_demo(normal)
    a.startup()
    assert not a.manager.has_active_meeting  # yarım kalan normal toplantı kurtarılmaz
    r = a.handle_text("Jarvis toplantıyı başlat")
    assert r.ok and "devred" not in r.message.lower()
    assert a.manager.repo.get_open_actions(exclude_meeting_id=a.manager.meeting.id, include_demo=True) == []
    r = a.handle_text("Jarvis önceki toplantıyı özetle")
    assert "Eski" not in r.message
    meeting = a.manager.meeting
    assert len(meeting.agenda) == 4 and sum(i.planned_minutes for i in meeting.agenda) == 20
    assert [p.name for p in meeting.participants] == [p["name"] for p in PEOPLE]
    assert not any(p.present for p in meeting.participants)  # yoklama sıfırdan
    assert not meeting.is_demo  # canlı toplantı: örnek veri değildir; izolasyonu ayrı veritabanı sağlar
    assert a.manager.repo.db_path.parent.parent == settings.data_dir / "demo_live"
    assert hashlib.sha256(settings.db_path.read_bytes()).hexdigest() == db_before


def test_new_demo_session_starts_clean(normal):
    _, first, _ = make_demo(normal, now=T0)
    first.handle_text("Jarvis toplantıyı başlat")
    first.handle_text("Jarvis görev: Ayşe demo görevini yapsın")  # birinci çekimde açık kalır, toplantı kapanmaz
    _, second, _ = make_demo(normal, now=T0 + timedelta(minutes=30))
    second.startup()
    assert not second.manager.has_active_meeting  # önceki demo oturumu da kurtarılmaz
    second.handle_text("Jarvis toplantıyı başlat")
    assert second.manager.repo.get_open_actions(exclude_meeting_id=second.manager.meeting.id,
                                                include_demo=True) == []


# ====================================================================== tam canlı akış
def test_full_demo_flow_pdf_mail_and_normal_untouched(normal, settings, monkeypatch, tmp_path):
    from PIL import Image

    no_smtp(monkeypatch)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (1200, 360), (120, 120, 120, 255)).save(logo)  # nötr test görseli
    db_before = hashlib.sha256(settings.db_path.read_bytes()).hexdigest()
    out_before = tree_fingerprint(settings.output_dir)
    demo, a, clock = make_demo(normal, logo=logo)
    a.handle_text("Jarvis toplantıyı başlat")
    a.handle_text("Jarvis Kenan, Ayşe ve Mehmet katıldı")
    a.handle_text("Öztürk")
    a.handle_text("evet")
    a.handle_text("Jarvis söz Ayşe'de")
    clock.advance(seconds=30)
    a.handle_text("Bu hafta üretim hedefimiz yüzde beş artış.")
    a.handle_text("Jarvis görev: Mehmet kalite raporunu cumaya kadar hazırlasın")
    a.handle_text("Mehmet Öztürk")
    a.handle_text("Mehmet Öztürk")  # tekrar: transkripte yazılmamalı (diyalog düzeltmesi korunur)
    a.handle_text("evet")
    a.handle_text("Jarvis karar: Yeni üretim planı pazartesi uygulanacak")
    a.handle_text("Jarvis oylama: fazla mesai lehte 3 aleyhte 0 çekimser 0")
    clock.advance(minutes=3, seconds=40)  # ilk madde 5 dk: ~4. dakikada tek seferlik %80 uyarısı
    first = a.handle_text("Jarvis gündemi oku")
    second = a.handle_text("Jarvis gündemi oku")
    assert "maddesi için yaklaşık 1 dakika kaldı" in first.message
    assert "kaldı" not in second.message.split("Gündemde")[0]
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert [e.text for e in stored.transcript] == ["Bu hafta üretim hedefimiz yüzde beş artış."]
    assert stored.action_items[-1].owner == "Mehmet Öztürk"
    assert stored.action_items[-1].due_date == date(2026, 10, 23)
    r = a.handle_text("Jarvis toplantıyı bitir")
    assert "Tutanak oluşturuldu" in r.message
    stored = a.manager.repo.get(a.manager.meeting.id)
    pdf = Path(stored.minutes_pdf)
    assert pdf.is_file() and demo.output_dir in pdf.parents
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(str(pdf))
    text = " ".join((p.extract_text() or "") for p in reader.pages)
    assert "TOPLANTI TUTANAĞI" in text and "ÖRNEK DEMİR ÇELİK" in text
    assert "Yeni üretim planı pazartesi uygulanacak" in text and "Mehmet Öztürk" in text
    assert "Eski karar" not in text and "eski raporu" not in text and "yarım toplantı" not in text
    assert "Devreden" not in text
    xobjects = reader.pages[0]["/Resources"].get("/XObject") or {}
    assert any(o.get_object().get("/Subtype") == "/Image" for o in xobjects.values())  # logo kullanıldı
    # manuel gönderim: gerçek SMTP yok, açık bilgilendirme ve .eml taslağı
    r = a.handle_text("Jarvis tutanağı gönder")
    assert "e-posta gönderilmedi" in r.message
    eml = sorted((demo.output_dir / "outbox").glob("*.eml"))
    assert eml and demo.output_dir in eml[0].parents
    saved = email.message_from_bytes(eml[0].read_bytes(), policy=policy.default)
    assert any((p.get_filename() or "").endswith(".pdf") for p in saved.iter_attachments())
    # normal ortam aynen korunur
    assert hashlib.sha256(settings.db_path.read_bytes()).hexdigest() == db_before
    assert tree_fingerprint(settings.output_dir) == out_before


def test_auto_send_disabled_even_if_env_enables(normal, monkeypatch):
    no_smtp(monkeypatch)
    _, a, _ = make_demo(normal)
    assert a.minutes.settings.auto_send_minutes is False and a.minutes.mailer.dry_run is True
    a.handle_text("Jarvis toplantıyı başlat")
    a.handle_text("Jarvis Kenan katıldı")
    r = a.handle_text("Jarvis toplantıyı bitir")
    assert "gönderildi" not in r.message


# ====================================================================== komut satırı
def test_cli_flag_only_with_demo_live():
    import main

    assert main.parse_args(["--demo-live"]).demo_live is True
    assert main.parse_args([]).demo_live is False
    assert main.parse_args(["--demo-live", "--text", "--no-tts"]).text is True


def test_normal_build_unchanged(settings):
    a = build_assistant(settings, use_tts=False, speech=SpeechEngine(enabled=False, output=io.StringIO()))
    assert a.minutes.settings.db_path == settings.db_path and a.minutes.demo is False
    assert a.manager.repo.db_path == settings.db_path and not a.manager.demo
