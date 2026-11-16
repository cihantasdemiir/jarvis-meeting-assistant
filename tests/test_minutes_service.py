"""Tutanak iş akışı: bitişte PDF, gönderim durumu, tekrar gönderimi önleme, ara tutanak (T-09, T-10)."""
from pathlib import Path

import pytest

from jarvis.mail.mailer import SendResult
from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.models import ActionStatus, MeetingStatus
from jarvis.reports.minutes_service import EmailStatus, MinutesService, slugify
from jarvis.reports.pdf_generator import PdfError
from jarvis.voice.listener import ScriptedListener

pypdf = pytest.importorskip("pypdf")


class FakeMailer:
    """SMTP yerine geçen taklit; davranışı test belirler."""

    def __init__(self, refuse: tuple[str, ...] = (), error: str = ""):
        self.calls: list[list[str]] = []
        self.refuse = refuse
        self.error = error
        self.dry_run = False
        self.sender = "jarvis@example.com"

    def send(self, message, recipients):
        self.calls.append(list(recipients))
        if self.error:
            return SendResult(False, "smtp", error=self.error)
        refused = {r: "550 reddedildi" for r in recipients if r in self.refuse}
        sent = [r for r in recipients if r not in refused]
        return SendResult(bool(sent) and not refused, "smtp", sent_to=sent, refused=refused)


def ended_meeting(manager: MeetingManager):
    manager.start_meeting()
    manager.mark_present(["Ayşe", "Mehmet"])
    manager.add_decision("Plan onaylandı")
    manager.end_meeting()
    return manager.meeting


def text_of(path: str) -> str:
    return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(path).pages)


def test_slugify():
    assert slugify("Şubat Ayı — Üretim/Planlama!") == "subat-ayi-uretim-planlama"
    assert slugify("!!!") == "toplanti"


def test_finalize_creates_pdf_and_records_path(manager, repo, settings, clock):
    service = MinutesService(repo, settings, mailer=FakeMailer(), clock=clock)
    meeting = ended_meeting(manager)
    result = service.finalize(meeting)
    assert result.ok and "Tutanak oluşturuldu" in result.message and "tutanağı gönder" in result.message
    stored = repo.get(meeting.id)
    assert Path(stored.minutes_pdf).is_file()
    assert stored.minutes_pdf.startswith(str(settings.output_dir))


def test_auto_send_when_enabled(manager, repo, settings, clock):
    mailer = FakeMailer()
    service = MinutesService(repo, settings.with_overrides(auto_send_minutes=True), mailer=mailer, clock=clock)
    result = service.finalize(ended_meeting(manager))
    assert result.ok and "4 alıcıya" in result.message
    assert len(mailer.calls) == 1


def test_auto_send_disabled_by_default(manager, repo, settings, clock):
    mailer = FakeMailer()
    MinutesService(repo, settings, mailer=mailer, clock=clock).finalize(ended_meeting(manager))
    assert mailer.calls == []


def test_demo_never_sends_even_with_auto_send(manager, repo, settings, clock):
    service = MinutesService(repo, settings.with_overrides(auto_send_minutes=True), clock=clock, demo=True)
    assert service.mailer.dry_run
    result = service.finalize(ended_meeting(manager))
    assert "e-postayla gönderildi" not in result.message


def test_duplicate_send_prevented_and_force_resends(manager, repo, settings, clock):
    mailer = FakeMailer()
    service = MinutesService(repo, settings, mailer=mailer, clock=clock)
    meeting = ended_meeting(manager)
    assert service.send_minutes(meeting).ok
    second = service.send_minutes(meeting)
    assert not second.ok and "zaten gönderildi" in second.message
    assert len(mailer.calls) == 1
    forced = service.send_minutes(meeting, force=True)
    assert forced.ok and len(mailer.calls) == 2
    stored = repo.get(meeting.id)
    assert stored.email_status["status"] == EmailStatus.SENT
    assert len(stored.email_status["attempts"]) == 2
    assert len(stored.email_status["pdf_sha256"]) == 64


def test_partial_failure_then_retry_only_pending(manager, repo, settings, clock):
    mailer = FakeMailer(refuse=("mehmet@example.com",))
    service = MinutesService(repo, settings, mailer=mailer, clock=clock)
    meeting = ended_meeting(manager)
    first = service.send_minutes(meeting)
    assert not first.ok and "mehmet@example.com" in first.message
    assert meeting.email_status["status"] == EmailStatus.PARTIAL
    assert "mehmet@example.com" in meeting.email_status["failed"]
    mailer.refuse = ()
    retry = service.send_minutes(meeting)
    assert retry.ok
    assert mailer.calls[-1] == ["mehmet@example.com"]  # yalnızca ulaşmamış adres
    assert meeting.email_status["status"] == EmailStatus.SENT
    assert meeting.email_status["failed"] == {}


def test_smtp_failure_keeps_meeting_and_pdf(manager, repo, settings, clock):
    service = MinutesService(repo, settings, mailer=FakeMailer(error="Sunucuya ulaşılamadı"), clock=clock)
    meeting = ended_meeting(manager)
    service.finalize(meeting)
    result = service.send_minutes(meeting)
    assert not result.ok and "PDF saklandı" in result.message
    stored = repo.get(meeting.id)
    assert stored.status == MeetingStatus.ENDED and Path(stored.minutes_pdf).is_file()
    assert stored.email_status["status"] == EmailStatus.FAILED


def test_pdf_failure_does_not_lose_meeting(manager, repo, settings, clock, monkeypatch):
    service = MinutesService(repo, settings, mailer=FakeMailer(), clock=clock)
    meeting = ended_meeting(manager)

    def broken(*args, **kwargs):
        raise PdfError("disk dolu (test)")

    monkeypatch.setattr(service.generator, "generate", broken)
    result = service.finalize(meeting)
    assert not result.ok and "tekrar deneyebilirsiniz" in result.message
    assert repo.get(meeting.id).status == MeetingStatus.ENDED


def test_send_regenerates_missing_pdf(manager, repo, settings, clock):
    service = MinutesService(repo, settings, mailer=FakeMailer(), clock=clock)
    meeting = ended_meeting(manager)
    service.finalize(meeting)
    Path(meeting.minutes_pdf).unlink()
    assert service.send_minutes(meeting).ok
    assert Path(meeting.minutes_pdf).is_file()


def test_no_send_during_active_meeting(manager, repo, settings, clock):
    mailer = FakeMailer()
    service = MinutesService(repo, settings, mailer=mailer, clock=clock)
    manager.start_meeting()
    assert not service.send_minutes(manager.meeting).ok
    assert mailer.calls == []


def test_redirect_test_mode_does_not_mark_sent(manager, repo, settings, clock):
    mailer = FakeMailer()
    service = MinutesService(repo, settings.with_overrides(mail_redirect_to="test@example.com"), mailer=mailer,
                             clock=clock)
    meeting = ended_meeting(manager)
    result = service.send_minutes(meeting)
    assert result.ok and "test@example.com" in result.message
    assert mailer.calls == [["test@example.com"]]
    assert meeting.email_status.get("sent_to", []) == []


def test_no_valid_recipients(manager, repo, settings, clock):
    settings.participants_file.write_text('[{"name": "Ayşe Yılmaz"}]', encoding="utf-8")
    service = MinutesService(repo, settings, mailer=FakeMailer(), clock=clock)
    result = service.send_minutes(ended_meeting(manager))
    assert not result.ok and "geçerli e-posta adresi yok" in result.message


def test_t09_completed_carryover_not_in_open_section(manager, repo, settings, clock):
    service = MinutesService(repo, settings, mailer=FakeMailer(), clock=clock)
    manager.start_meeting()
    manager.add_action("Mehmet raporu haftaya kadar hazırlasın")
    manager.add_action("Ayşe sunumu cumaya kadar hazırlasın")
    manager.end_meeting()

    clock.advance(days=7)
    second = MeetingManager(repo, settings, clock=clock)
    second.start_meeting()
    second.complete_action("rapor")
    second.end_meeting()
    path = service.create_pdf(second.meeting).data["path"]
    text = text_of(path)
    open_part = text.split("Açık devreden görevler")[1].split("Bu toplantıda kapatılan")[0]
    closed_part = text.split("Bu toplantıda kapatılan devreden görevler")[1]
    assert "sunumu hazırlasın" in open_part and "raporu hazırlasın" not in open_part
    assert "raporu hazırlasın" in closed_part
    assert repo.get_actions(second.meeting.carryover_completed)[0].status == ActionStatus.DONE


def test_t10_ctrl_c_saves_meeting_and_creates_interim_pdf(assistant, repo, console):
    """T-10: Ctrl+C -> toplantı kaybolmaz, ara tutanak üretilir, toplantı açık kalır (kurtarılabilir)."""

    class InterruptingListener(ScriptedListener):
        def listen(self):
            line = super().listen()
            if line is None:
                raise KeyboardInterrupt
            return line

    assistant.run(InterruptingListener(["Jarvis toplantıyı başlat", "Jarvis söz Ayşe'de", "Kesinti öncesi söz.",
                                        "Jarvis karar: Ara tutanak test kararı"]), monitor_interval=None)
    output = console.getvalue()
    assert "Ara tutanak oluşturuldu" in output
    active = repo.get_active()
    assert active is not None and active.status == MeetingStatus.ACTIVE
    interim = list(Path(assistant.minutes.settings.output_dir).glob("*_ARA-TUTANAK.pdf"))
    assert len(interim) == 1
    text = text_of(str(interim[0]))
    assert "ARA TUTANAK" in text and "Ara tutanak test kararı" in text and "Kesinti öncesi söz." in text


def test_end_meeting_via_assistant_produces_pdf(assistant, repo):
    assistant.handle_text("Jarvis toplantıyı başlat")
    response = assistant.handle_text("Jarvis toplantıyı bitir")
    assert "Toplantı sona erdi" in response.message and "Tutanak oluşturuldu" in response.message
    meeting = repo.get(assistant.manager.meeting.id)
    assert Path(meeting.minutes_pdf).is_file()


def test_send_and_create_commands_via_assistant(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    interim = assistant.handle_text("Jarvis tutanak oluştur")
    assert interim.ok and "Ara tutanak" in interim.message
    assistant.handle_text("Jarvis toplantıyı bitir")
    sent = assistant.handle_text("Jarvis tutanağı gönder")
    assert sent.ok and "Deneme kipi" in sent.message  # test ortamında dry-run


def test_send_after_restart_uses_last_meeting(assistant, repo, settings, clock, minutes):
    from jarvis.assistant import JarvisAssistant
    from jarvis.voice.command_parser import CommandParser
    from jarvis.voice.speech_engine import SpeechEngine

    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis toplantıyı bitir")
    restarted = JarvisAssistant(MeetingManager(repo, settings, clock=clock), CommandParser(),
                                SpeechEngine(enabled=False), minutes=minutes)
    response = restarted.handle_text("Jarvis tutanağı gönder")
    assert response.ok and "Deneme kipi" in response.message
