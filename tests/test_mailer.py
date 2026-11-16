"""E-posta testleri (G-11, T-07). Gerçek dış SMTP sunucusu kullanılmaz.

Uçtan uca testler bilgisayarın kendi üzerinde (127.0.0.1) çalışan geçici bir
SMTP sunucusuyla (aiosmtpd) yapılır; e-posta internete çıkmaz.
"""
import email
import smtplib
import socket
import threading
from datetime import datetime
from email import policy
from pathlib import Path
from unittest import mock

import pytest

from config.settings import Settings
from jarvis.mail.mailer import Mailer, Recipient, build_minutes_message, collect_recipients, subject_for
from jarvis.meeting.models import ActionItem, Decision, Meeting, MeetingStatus, Participant

T0 = datetime(2026, 10, 14, 10, 0, 0)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_meeting() -> Meeting:
    return Meeting(
        title="Bütçe Görüşmesi — Çeyrek Değerlendirmesi", started_at=T0, status=MeetingStatus.ENDED,
        participants=[Participant("Ayşe Yılmaz", "ayse@example.com", present=True),
                      Participant("Mehmet Öztürk", "MEHMET@example.com", present=False),
                      Participant("Kopya Mehmet", "mehmet@example.com", present=True),
                      Participant("Dış Danışman", "danisman@example.org", present=True),
                      Participant("Adresi Yok", "", present=True),
                      Participant("Bozuk Adres", "bozuk@adres", present=True)],
        decisions=[Decision("Bütçe %5 artırılacak")],
        action_items=[ActionItem("Raporu hazırla", "Ayşe Yılmaz")],
        summary="Özet: bütçe görüşüldü.",
    )


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    path = tmp_path / "tutanak.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    return path


# ------------------------------------------------------------------ alıcı doğrulama
def test_recipients_validated_and_deduplicated():
    result = collect_recipients(make_meeting(), Settings())
    assert result.emails == ["ayse@example.com", "MEHMET@example.com", "danisman@example.org"]
    reasons = dict(result.skipped)
    assert "e-posta adresi yok" in reasons["Adresi Yok"]
    assert "geçersiz" in reasons["Bozuk Adres"]


def test_recipients_allowed_domains():
    result = collect_recipients(make_meeting(), Settings(mail_allowed_domains=["example.com"]))
    assert "danisman@example.org" not in result.emails
    assert any("izin verilmeyen" in reason for _, reason in result.skipped)


def test_recipients_present_only():
    result = collect_recipients(make_meeting(), Settings(mail_recipients_mode="present"))
    assert "MEHMET@example.com" not in result.emails
    assert "mehmet@example.com" in result.emails


# ------------------------------------------------------------------ mesaj
def test_message_structure(pdf_file):
    meeting = make_meeting()
    message = build_minutes_message(meeting, pdf_file, "jarvis@example.com",
                                    [Recipient("Ayşe Yılmaz", "ayse@example.com")])
    parsed = email.message_from_bytes(bytes(message), policy=policy.default)
    assert parsed["Subject"] == "J.A.R.V.I.S — Bütçe Görüşmesi — Çeyrek Değerlendirmesi — Toplantı Tutanağı"
    assert "Ayşe Yılmaz" in parsed["To"]
    assert "Bütçe %5 artırılacak" in parsed.get_body(("plain",)).get_content()
    assert "<li>Bütçe %5 artırılacak</li>" in parsed.get_body(("html",)).get_content()
    attachments = list(parsed.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["tutanak.pdf"]
    assert attachments[0].get_content_type() == "application/pdf"


def test_html_is_escaped(pdf_file):
    meeting = make_meeting()
    meeting.decisions = [Decision("<script>alert(1)</script>")]
    message = build_minutes_message(meeting, pdf_file, "j@example.net", [Recipient("A", "a@example.net")])
    html = email.message_from_bytes(bytes(message), policy=policy.default).get_body(("html",)).get_content()
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_test_subject_prefix():
    assert subject_for(make_meeting(), test=True).startswith("[TEST] J.A.R.V.I.S")


# ------------------------------------------------------------------ yerel SMTP sunucusu (uçtan uca)
class RecordingHandler:
    def __init__(self, reject: tuple[str, ...] = ()):
        self.envelopes = []
        self.reject = reject

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
        if address.lower() in self.reject:
            return "550 5.1.1 Kullanici yok"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        self.envelopes.append(envelope)
        return "250 Mesaj kabul edildi"


@pytest.fixture
def smtp_server():
    controller_mod = pytest.importorskip("aiosmtpd.controller")
    servers = []

    def start(reject: tuple[str, ...] = ()):
        handler = RecordingHandler(reject)
        controller = controller_mod.Controller(handler, hostname="127.0.0.1", port=free_port())
        controller.start()
        servers.append(controller)
        return handler, controller.port

    yield start
    for controller in servers:
        controller.stop()


def smtp_settings(port: int, tmp_path: Path, **overrides) -> Settings:
    return Settings(smtp_host="127.0.0.1", smtp_port=port, smtp_use_tls=False, smtp_from="jarvis@example.com",
                    smtp_timeout=5, output_dir=tmp_path, **overrides)


def test_t07_end_to_end_delivery(smtp_server, pdf_file, tmp_path):
    """T-07: PDF ekli e-posta, katılımcı adreslerine Türkçe konu satırıyla ulaşır."""
    handler, port = smtp_server()
    mailer = Mailer(smtp_settings(port, tmp_path))
    meeting = make_meeting()
    recipients = collect_recipients(meeting, mailer.settings).recipients
    message = build_minutes_message(meeting, pdf_file, mailer.sender, recipients)
    result = mailer.send(message, [r.email for r in recipients])
    assert result.ok and result.mode == "smtp"
    assert sorted(result.sent_to) == sorted(r.email for r in recipients)
    envelope = handler.envelopes[0]
    assert sorted(envelope.rcpt_tos) == sorted(r.email for r in recipients)
    received = email.message_from_bytes(envelope.content, policy=policy.default)
    assert received["Subject"] == subject_for(meeting)
    attachment = next(received.iter_attachments())
    assert attachment.get_payload(decode=True) == pdf_file.read_bytes()


def test_partially_refused_recipients(smtp_server, pdf_file, tmp_path):
    handler, port = smtp_server(reject=("danisman@example.org",))
    mailer = Mailer(smtp_settings(port, tmp_path))
    recipients = ["ayse@example.com", "danisman@example.org"]
    message = build_minutes_message(make_meeting(), pdf_file, mailer.sender, [Recipient("x", r) for r in recipients])
    result = mailer.send(message, recipients)
    assert not result.ok
    assert result.sent_to == ["ayse@example.com"]
    assert "550" in result.refused["danisman@example.org"]


def test_all_recipients_refused(smtp_server, pdf_file, tmp_path):
    _, port = smtp_server(reject=("ayse@example.com",))
    mailer = Mailer(smtp_settings(port, tmp_path))
    message = build_minutes_message(make_meeting(), pdf_file, mailer.sender, [Recipient("x", "ayse@example.com")])
    result = mailer.send(message, ["ayse@example.com"])
    assert not result.ok and not result.sent_to and "reddetti" in result.error


def test_connection_refused(pdf_file, tmp_path):
    mailer = Mailer(smtp_settings(free_port(), tmp_path))
    message = build_minutes_message(make_meeting(), pdf_file, mailer.sender, [Recipient("x", "a@example.net")])
    result = mailer.send(message, ["a@example.net"])
    assert not result.ok and "ulaşılamadı" in result.error


def test_server_not_responding_times_out(pdf_file, tmp_path):
    """Sunucu bağlantıyı kabul edip hiç yanıt vermezse program asılı kalmaz."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept_and_hang():
        conn, _ = listener.accept()
        stop.wait(10)
        conn.close()

    thread = threading.Thread(target=accept_and_hang, daemon=True)
    thread.start()
    try:
        settings = Settings(smtp_host="127.0.0.1", smtp_port=port, smtp_use_tls=False, smtp_from="j@example.net",
                            smtp_timeout=1, output_dir=tmp_path)
        mailer = Mailer(settings)
        message = build_minutes_message(make_meeting(), pdf_file, "j@example.net", [Recipient("x", "a@example.net")])
        result = mailer.send(message, ["a@example.net"])
        assert not result.ok and result.error
    finally:
        stop.set()
        listener.close()


# ------------------------------------------------------------------ TLS / SSL / kimlik doğrulama (taklit sunucu)
def _fake_server():
    server = mock.MagicMock()
    server.send_message.return_value = {}
    return server


def test_starttls_and_login_used(pdf_file, tmp_path):
    settings = Settings(smtp_host="smtp.example.com", smtp_port=587, smtp_user="jarvis@example.com",
                        smtp_password="gizli", smtp_use_tls=True, output_dir=tmp_path)
    server = _fake_server()
    with mock.patch("smtplib.SMTP", return_value=server) as smtp_cls:
        result = Mailer(settings).send(
            build_minutes_message(make_meeting(), pdf_file, "jarvis@example.com", [Recipient("x", "a@example.net")]),
            ["a@example.net"])
    assert result.ok
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("jarvis@example.com", "gizli")


def test_ssl_port_uses_smtp_ssl(pdf_file, tmp_path):
    settings = Settings(smtp_host="smtp.example.com", smtp_port=465, smtp_from="j@example.com", smtp_use_ssl=True,
                        output_dir=tmp_path)
    server = _fake_server()
    with mock.patch("smtplib.SMTP_SSL", return_value=server) as ssl_cls, mock.patch("smtplib.SMTP") as plain:
        result = Mailer(settings).send(
            build_minutes_message(make_meeting(), pdf_file, "j@example.com", [Recipient("x", "a@example.net")]),
            ["a@example.net"])
    assert result.ok
    ssl_cls.assert_called_once()
    plain.assert_not_called()
    server.starttls.assert_not_called()


def test_wrong_password_reported(pdf_file, tmp_path):
    settings = Settings(smtp_host="smtp.example.com", smtp_user="u@example.com", smtp_password="yanlis",
                        output_dir=tmp_path)
    server = _fake_server()
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Kimlik hatasi")
    with mock.patch("smtplib.SMTP", return_value=server):
        result = Mailer(settings).send(
            build_minutes_message(make_meeting(), pdf_file, "u@example.com", [Recipient("x", "a@example.net")]),
            ["a@example.net"])
    assert not result.ok and "şifresi hatalı" in result.error
    server.quit.assert_called_once()  # bağlantı her durumda kapatılır


@pytest.mark.parametrize("overrides,expected", [
    ({}, "SMTP_HOST"),
    ({"smtp_host": "smtp.x.com"}, "SMTP_FROM"),
    ({"smtp_host": "smtp.x.com", "smtp_user": "u@example.net"}, "SMTP_PASSWORD"),
])
def test_missing_config_reported(pdf_file, tmp_path, overrides, expected):
    mailer = Mailer(Settings(output_dir=tmp_path, **overrides))
    message = build_minutes_message(make_meeting(), pdf_file, mailer.sender, [Recipient("x", "a@example.net")])
    result = mailer.send(message, ["a@example.net"])
    assert not result.ok and expected in result.error


def test_dry_run_writes_eml_without_network(pdf_file, tmp_path):
    mailer = Mailer(Settings(output_dir=tmp_path), dry_run=True)
    with mock.patch("smtplib.SMTP") as smtp_cls:
        result = mailer.send(build_minutes_message(make_meeting(), pdf_file, mailer.sender,
                                                   [Recipient("x", "a@example.net")]), ["a@example.net"])
    smtp_cls.assert_not_called()
    assert result.ok and result.mode == "dry-run"
    saved = email.message_from_bytes(Path(result.eml_path).read_bytes(), policy=policy.default)
    assert saved["Subject"] == subject_for(make_meeting())
