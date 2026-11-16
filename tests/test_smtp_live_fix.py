"""smtp_live_fix: Gmail benzeri gerçek SMTP akışı, aşama tanılaması ve --smtp-check.

Tüm testler 127.0.0.1 üzerindeki senaryolu sahte sunucuyla yapılır (`tests/smtp_fakes.py`); gerçek Gmail'e
bağlanılmaz, e-posta gönderilmez.
"""
from __future__ import annotations

import email
import logging
import smtplib
from email import policy
from pathlib import Path
from unittest import mock

import pytest

from config.settings import Settings
from jarvis.mail.mailer import Mailer, Recipient, Stage, build_minutes_message, describe_settings, mask_address
from jarvis.meeting.models import Meeting, MeetingStatus, Participant
from jarvis.reports.minutes_service import MinutesService
from tests.smtp_fakes import FakeSmtpServer, Script, client_context

USER, PASSWORD = "jarvis.test@example.com", "abcdefghijklmnop"


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    path = tmp_path / "tutanak.pdf"
    path.write_bytes(b"%PDF-1.4 ornek tutanak \xc3\xa7\xc4\x9f")
    return path


@pytest.fixture
def fake():
    servers = []

    def start(**script):
        server = FakeSmtpServer(Script(**script))
        servers.append(server)
        return server
    yield start
    for server in servers:
        server.close()


def settings_for(server, tmp_path, **overrides) -> Settings:
    base = dict(smtp_host="localhost", smtp_port=server.port, smtp_user=USER, smtp_password=PASSWORD,
                smtp_from=USER, smtp_use_tls=True, smtp_use_ssl=False, smtp_timeout=5, output_dir=tmp_path)
    base.update(overrides)
    return Settings(**base)


def meeting() -> Meeting:
    return Meeting(title="Haftalık Toplantı — Çalışma", status=MeetingStatus.ENDED,
                   participants=[Participant("Test Alıcı", "alici01@example.com", present=True)])


def send(mailer: Mailer, pdf_file: Path, recipients=("alici01@example.com",)):
    message = build_minutes_message(meeting(), pdf_file, mailer.sender, [Recipient("x", r) for r in recipients])
    return mailer.send(message, list(recipients))


def mailer_for(server, tmp_path, **overrides) -> Mailer:
    return Mailer(settings_for(server, tmp_path, **overrides), ssl_context=client_context())


# ====================================================================== kök neden
def test_root_cause_smtplib_masks_auth_failure_when_server_closes(fake):
    """smtplib.login PLAIN reddedilince LOGIN'i dener; sunucu bağlantıyı kapattıysa asıl 535 kaybolur."""
    server = fake(tls=False, close_after_auth_fail=True)
    client = smtplib.SMTP("localhost", server.port, timeout=5)
    client.ehlo()
    with pytest.raises(smtplib.SMTPServerDisconnected, match="Connection unexpectedly closed"):
        client.login(USER, "yanlis")


def test_fix_reports_real_535_instead_of_disconnect(fake, pdf_file, tmp_path):
    server = fake(close_after_auth_fail=True)
    with mock.patch.object(Mailer, "_is_gmail", new=property(lambda self: True)):
        result = send(mailer_for(server, tmp_path, smtp_password="yanlisyanlisyanl"), pdf_file)
    assert not result.ok and result.stage == Stage.LOGIN
    assert "535" in result.error and "uygulama şifresi" in result.error and "şifresi hatalı" in result.error
    assert server.auth_attempts == 1  # ikinci yöntem denenmedi
    assert server.messages == []


# ====================================================================== başarılı akış ve sıra
def test_starttls_flow_order_and_pdf_attachment(fake, pdf_file, tmp_path):
    server = fake()
    result = send(mailer_for(server, tmp_path), pdf_file)
    assert result.ok and result.sent_to == ["alici01@example.com"] and result.stage == ""
    verbs = [c.split(" ", 1)[0].upper() for c in server.commands]
    assert verbs[:5] == ["EHLO", "STARTTLS", "EHLO", "AUTH", "MAIL"]
    assert verbs[5:] == ["RCPT", "DATA", "QUIT"]
    assert "250 2.0.0 OK" in result.accepted_reply  # sunucu kabulü; gelen kutusu ayrıca doğrulanır
    received = email.message_from_bytes(server.messages[0], policy=policy.default)
    attachment = next(received.iter_attachments())
    assert attachment.get_filename() == "tutanak.pdf"
    assert attachment.get_payload(decode=True) == pdf_file.read_bytes()


def test_direct_ssl_465_alternative(fake, pdf_file, tmp_path):
    server = fake(direct_ssl=True, tls=False)
    result = send(mailer_for(server, tmp_path, smtp_use_ssl=True, smtp_use_tls=False), pdf_file)
    assert result.ok and "STARTTLS" not in " ".join(server.commands)


def test_gmail_app_password_with_spaces(fake, pdf_file, tmp_path):
    server = fake()
    mailer = mailer_for(server, tmp_path, smtp_password="abcd efgh ijkl mnop")
    mailer.settings = mailer.settings.with_overrides(smtp_host="localhost")
    with mock.patch.object(Mailer, "_is_gmail", new=property(lambda self: True)):
        assert send(mailer, pdf_file).ok


def test_non_gmail_password_spaces_untouched(fake, pdf_file, tmp_path):
    server = fake(password="abcd efgh ijkl mnop")
    assert send(mailer_for(server, tmp_path, smtp_password="abcd efgh ijkl mnop"), pdf_file).ok


# ====================================================================== hatalar ve aşamalar
@pytest.mark.parametrize("code,text,needle", [
    (535, "5.7.8 Username and Password not accepted", "şifresi hatalı"),
    (534, "5.7.9 Application-specific password required", "uygulama şifresi gerekiyor"),
    (534, "5.7.14 Please log in via your web browser", "güvenlik uyarısını"),
    (454, "4.7.0 Too many login attempts", "Birkaç dakika bekleyin"),
])
def test_auth_errors_are_explained(fake, pdf_file, tmp_path, code, text, needle):
    server = fake(auth_fail=(code, text))
    with mock.patch.object(Mailer, "_is_gmail", new=property(lambda self: True)):
        result = send(mailer_for(server, tmp_path), pdf_file)
    assert not result.ok and result.stage == Stage.LOGIN and needle in result.error


def test_tls_handshake_failure(fake, pdf_file, tmp_path):
    server = fake(broken_tls=True)
    result = send(mailer_for(server, tmp_path), pdf_file)
    assert not result.ok and result.stage == Stage.STARTTLS and "TLS" in result.error


def test_untrusted_certificate_is_rejected(fake, pdf_file, tmp_path):
    server = fake()
    result = send(Mailer(settings_for(server, tmp_path)), pdf_file)  # sistem deposu: test sertifikası güvenilmez
    assert not result.ok and result.stage == Stage.STARTTLS


@pytest.mark.parametrize("close_on,stage", [
    ({"AUTH"}, Stage.LOGIN), ({"MAIL"}, Stage.MAIL_FROM), ({"RCPT"}, Stage.RCPT_TO), ({"DATA"}, Stage.DATA),
    ({"DATA_END"}, Stage.DATA), ({"STARTTLS"}, Stage.STARTTLS),
])
def test_disconnect_stage_is_identified(fake, pdf_file, tmp_path, close_on, stage):
    server = fake(close_on=close_on)
    result = send(mailer_for(server, tmp_path), pdf_file)
    assert not result.ok and result.stage == stage and f"[{stage}]" in result.error
    assert server.messages == [] and not result.sent_to
    if stage in (Stage.MAIL_FROM, Stage.RCPT_TO, Stage.DATA):
        assert "KABUL EDİLMEDİ" in result.error


def test_wrong_recipient(fake, pdf_file, tmp_path):
    server = fake(reject_rcpt={"yok@example.com": "550 5.1.1 The email account does not exist"})
    result = send(mailer_for(server, tmp_path), pdf_file, recipients=("yok@example.com",))
    assert not result.ok and "550" in result.refused["yok@example.com"] and result.stage == Stage.RCPT_TO


def test_partial_recipient_refusal(fake, pdf_file, tmp_path):
    server = fake(reject_rcpt={"yok@example.com": "550 5.1.1 no such user"})
    result = send(mailer_for(server, tmp_path), pdf_file, recipients=("alici01@example.com", "yok@example.com"))
    assert result.sent_to == ["alici01@example.com"] and "yok@example.com" in result.refused


def test_starttls_required_but_disabled(fake, pdf_file, tmp_path):
    server = fake()
    result = send(mailer_for(server, tmp_path, smtp_use_tls=False), pdf_file)
    assert not result.ok and result.stage == Stage.LOGIN and "SMTP_USE_TLS=true" in result.error


def test_non_ascii_password_explained(fake, pdf_file, tmp_path):
    server = fake()
    result = send(mailer_for(server, tmp_path, smtp_password="şifreğüı"), pdf_file)
    assert not result.ok and result.stage == Stage.LOGIN and "ASCII" in result.error


def test_secrets_and_content_never_logged(fake, pdf_file, tmp_path, caplog):
    server = fake(close_after_auth_fail=True)
    caplog.set_level(logging.DEBUG)
    send(mailer_for(server, tmp_path, smtp_password="gizligizligizlig"), pdf_file)
    logged = caplog.text
    assert "gizligizligizlig" not in logged and USER not in logged
    assert "Haftalık Toplantı" not in logged and "aşama: GİRİŞ" in logged


# ====================================================================== --smtp-check
def test_smtp_check_never_sends(fake, tmp_path):
    server = fake()
    result = mailer_for(server, tmp_path).check_connection()
    assert result.ok
    assert [s.stage for s in result.steps] == [Stage.CONNECT, Stage.EHLO, Stage.STARTTLS, Stage.EHLO_TLS,
                                               Stage.LOGIN, Stage.QUIT]
    verbs = {c.split(" ", 1)[0].upper() for c in server.commands}
    assert not verbs & {"MAIL", "RCPT", "DATA"} and server.messages == []


def test_smtp_check_reports_failing_stage(fake, tmp_path):
    server = fake(close_after_auth_fail=True)
    result = mailer_for(server, tmp_path, smtp_password="yanlisyanlisyanl").check_connection()
    assert not result.ok and result.steps[-1].stage == Stage.LOGIN and not result.steps[-1].ok
    assert "535" in result.steps[-1].detail and "535" in result.error


def test_smtp_check_works_even_in_dry_run(fake, tmp_path):
    server = fake()
    mailer = mailer_for(server, tmp_path, mail_dry_run=True)
    assert mailer.check_connection().ok and mailer.dry_run is True  # gönderim kipi değişmez


def test_smtp_check_missing_config(tmp_path):
    result = Mailer(Settings(output_dir=tmp_path)).check_connection()
    assert not result.ok and "SMTP_HOST" in result.error


def test_describe_settings_masks_secrets():
    settings = Settings(smtp_host="smtp.gmail.com", smtp_port=587, smtp_user="ornek.kullanici@example.com",
                        smtp_password="abcd efgh ijkl mnop", smtp_from="ornek.kullanici@example.com",
                        mail_redirect_to="alici01@example.com")
    text = "\n".join(describe_settings(settings))
    assert "abcd" not in text and "ornekkullanici" not in text and "ogrenci01" not in text
    assert "uygulama şifresi biçiminde" in text and mask_address("ornek.kullanici@example.com") == "o***i@example.com"
    wrong = "\n".join(describe_settings(settings.with_overrides(smtp_password="normalSifre1", smtp_port=465)))
    assert "biçiminde değil" in wrong and "SMTP_USE_SSL=true" in wrong


def test_cli_smtp_check_flag():
    import main
    assert main.parse_args(["--smtp-check", "--smtp-ssl"]).smtp_check is True


# ====================================================================== mevcut kısıtlar korunur
def test_dry_run_never_connects(pdf_file, tmp_path):
    mailer = Mailer(Settings(output_dir=tmp_path, smtp_host="smtp.gmail.com", mail_dry_run=True))
    with mock.patch("smtplib.SMTP") as plain, mock.patch("smtplib.SMTP_SSL") as secure:
        result = send(mailer, pdf_file)
    plain.assert_not_called()
    secure.assert_not_called()
    assert result.ok and result.mode == "dry-run" and Path(result.eml_path).is_file()


def _ended_meeting(tmp_path, pdf_file) -> Meeting:
    m = Meeting(title="T", status=MeetingStatus.ENDED, minutes_pdf=str(pdf_file),
                participants=[Participant("A", "a@example.com", present=True),
                              Participant("B", "b@example.org", present=True)])
    return m


def test_redirect_sends_only_to_test_address(fake, pdf_file, tmp_path, repo):
    server = fake()
    settings = settings_for(server, tmp_path, mail_redirect_to="alici01@example.com")
    service = MinutesService(repo, settings, mailer=Mailer(settings, ssl_context=client_context()))
    result = service.send_minutes(_ended_meeting(tmp_path, pdf_file))
    assert result.ok and "sunucu kabul etti" in result.message and "gelen kutusundan" in result.message
    rcpts = [c.lower() for c in server.commands if c.upper().startswith("RCPT")]
    assert rcpts == ["rcpt to:<alici01@example.com>"]


def test_allowed_domains_still_filter(fake, pdf_file, tmp_path, repo):
    server = fake()
    settings = settings_for(server, tmp_path, mail_allowed_domains=["example.com"])
    service = MinutesService(repo, settings, mailer=Mailer(settings, ssl_context=client_context()))
    meeting_ = _ended_meeting(tmp_path, pdf_file)
    result = service.send_minutes(meeting_)
    rcpts = [c.lower() for c in server.commands if c.upper().startswith("RCPT")]
    assert rcpts == ["rcpt to:<a@example.com>"] and "izin verilmeyen alan adı" in result.message


def test_failed_send_records_stage_and_keeps_pdf(fake, pdf_file, tmp_path, repo):
    server = fake(close_on={"DATA_END"})
    settings = settings_for(server, tmp_path)
    service = MinutesService(repo, settings, mailer=Mailer(settings, ssl_context=client_context()))
    meeting_ = _ended_meeting(tmp_path, pdf_file)
    result = service.send_minutes(meeting_)
    assert not result.ok and pdf_file.is_file()
    attempt = meeting_.email_status["attempts"][-1]
    assert attempt["stage"] == Stage.DATA and meeting_.email_status["sent_to"] == []
