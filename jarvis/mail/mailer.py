"""E-posta gönderimi (G-11).

  * SMTP: STARTTLS (587) veya doğrudan SSL (465); yalnızca standart kütüphane.
  * Gövde: düz metin + HTML alternatifi; PDF tutanak (ve ileride .ics) eki.
  * Alıcılar katılımcı listesinden alınır ve doğrulanır:
      - geçersiz adresler ve tekrarlar ayıklanır,
      - MAIL_ALLOWED_DOMAINS doluysa yalnızca bu alan adlarına gönderilir,
      - MAIL_REDIRECT_TO doluysa tüm e-postalar yalnızca test adresine gider.
  * Dry-run: SMTP yerine .eml dosyası yazılır (demo ve kurulum testleri için).

Bu modül toplantı durumunu değiştirmez; sonucu `SendResult` olarak döndürür.

SMTP aşamaları (smtp_live_fix): BAĞLANTI → EHLO → STARTTLS → EHLO (TLS sonrası) → GİRİŞ → MAIL FROM → RCPT TO →
DATA → QUIT. Hata olursa hangi aşamada olduğu `SendResult.stage` ve günlükte yazılır. Kullanıcı adı, parola ve
e-posta içeriği hiçbir zaman günlüğe yazılmaz; yalnızca aşama adı, SMTP yanıt kodu ve sunucunun kısa yanıtı yazılır.

Kök neden notu: `smtplib.SMTP.login()` sunucunun ilan ettiği her yöntemi sırayla dener (PLAIN, sonra LOGIN).
Sunucu reddedilen girişten sonra bağlantıyı kapatırsa ikinci deneme "Connection unexpectedly closed" verir ve
asıl 535/534 yanıtı kaybolur. Bu yüzden giriş TEK yöntemle (önce PLAIN, yoksa LOGIN) yapılır.
"""
from __future__ import annotations

import logging
import mimetypes
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path

from config.settings import Settings

from ..meeting.models import ActionStatus, Meeting
from ..utils.dates import format_date_time, format_tr_date, format_tr_short

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class MailConfigError(RuntimeError):
    """SMTP ayarları eksik veya hatalı olduğunda fırlatılır."""


@dataclass
class Recipient:
    name: str
    email: str


@dataclass
class RecipientList:
    recipients: list[Recipient] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (kişi, neden)

    @property
    def emails(self) -> list[str]:
        return [r.email for r in self.recipients]


@dataclass
class SendResult:
    ok: bool
    mode: str  # "smtp" | "dry-run"
    sent_to: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    error: str = ""
    eml_path: str = ""
    stage: str = ""  # hata olan SMTP aşaması (başarıda boş)
    # Sunucunun DATA sonrası kabul yanıtı (ör. "250 2.0.0 OK ... gsmtp"). Bu, sunucunun iletiyi KABUL ettiğini
    # gösterir; alıcının gelen kutusuna ulaştığını değil (o ayrıca alıcıdan doğrulanmalıdır).
    accepted_reply: str = ""


class Stage:
    CONNECT = "BAĞLANTI"
    EHLO = "EHLO"
    STARTTLS = "STARTTLS"
    EHLO_TLS = "EHLO (TLS sonrası)"
    LOGIN = "GİRİŞ (kimlik doğrulama)"
    MAIL_FROM = "MAIL FROM"
    RCPT_TO = "RCPT TO"
    DATA = "DATA (ileti gönderimi)"
    QUIT = "QUIT"


class SmtpStageError(Exception):
    """Bir SMTP aşamasında oluşan hatayı aşama bilgisiyle sarar."""

    def __init__(self, stage: str, original: BaseException):
        super().__init__(f"{stage}: {original}")
        self.stage = stage
        self.original = original


@dataclass
class CheckStep:
    stage: str
    ok: bool
    detail: str = ""


@dataclass
class CheckResult:
    ok: bool
    steps: list[CheckStep] = field(default_factory=list)
    error: str = ""


def subject_for(meeting: Meeting, test: bool = False) -> str:
    subject = f"J.A.R.V.I.S — {meeting.title} — Toplantı Tutanağı"
    return f"[TEST] {subject}" if test else subject


def collect_recipients(meeting: Meeting, settings: Settings) -> RecipientList:
    """Katılımcı listesinden alıcıları çıkarır ve doğrular."""
    result = RecipientList()
    seen: set[str] = set()
    allowed = [d.lower() for d in settings.mail_allowed_domains]
    for participant in meeting.participants:
        if settings.mail_recipients_mode == "present" and not participant.present:
            continue
        email = (participant.email or "").strip()
        if not email:
            result.skipped.append((participant.name, "e-posta adresi yok"))
            continue
        if not EMAIL_RE.match(email):
            result.skipped.append((participant.name, f"geçersiz adres: {email}"))
            continue
        key = email.lower()
        if key in seen:
            continue
        domain = key.rsplit("@", 1)[1]
        if allowed and domain not in allowed:
            result.skipped.append((participant.name, f"izin verilmeyen alan adı: {domain}"))
            continue
        seen.add(key)
        result.recipients.append(Recipient(participant.name, email))
    return result


# ---------------------------------------------------------------------- gövde
def _plain_body(meeting: Meeting) -> str:
    lines = [
        "Merhaba,",
        "",
        f"{format_tr_date(meeting.started_at.date())} tarihli \"{meeting.title}\" toplantısının tutanağı ektedir.",
        "",
    ]
    if meeting.summary:
        lines += ["Özet:", meeting.summary, ""]
    if meeting.decisions:
        lines.append("Alınan kararlar:")
        lines += [f"  {i}. {d.text}" for i, d in enumerate(meeting.decisions, start=1)]
        lines.append("")
    open_actions = [a for a in meeting.action_items if a.status == ActionStatus.OPEN]
    if open_actions:
        lines.append("Görevler:")
        lines += [f"  - {a.owner or 'Sorumlu belirlenmedi'}: {a.text} (son tarih: {format_tr_short(a.due_date)})"
                  for a in open_actions]
        lines.append("")
    if meeting.next_meeting_date:
        lines += [f"Sonraki toplantı: {format_date_time(meeting.next_meeting_date, meeting.next_meeting_time)}", ""]
    lines += ["Bu e-posta J.A.R.V.I.S Toplantı Asistanı tarafından otomatik olarak gönderilmiştir."]
    return "\n".join(lines)


def _html_body(meeting: Meeting, color: str) -> str:
    def h(text: object) -> str:
        return escape(str(text))

    parts = [
        "<!doctype html><html><body style=\"font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222\">",
        f"<p>Merhaba,</p><p><b>{h(format_tr_date(meeting.started_at.date()))}</b> tarihli "
        f"<b>{h(meeting.title)}</b> toplantısının tutanağı ektedir.</p>",
    ]
    if meeting.summary:
        parts.append(f"<h3 style=\"color:{h(color)}\">Özet</h3><p>{h(meeting.summary)}</p>")
    if meeting.decisions:
        parts.append(f"<h3 style=\"color:{h(color)}\">Alınan kararlar</h3><ol>")
        parts += [f"<li>{h(d.text)}</li>" for d in meeting.decisions]
        parts.append("</ol>")
    open_actions = [a for a in meeting.action_items if a.status == ActionStatus.OPEN]
    if open_actions:
        parts.append(f"<h3 style=\"color:{h(color)}\">Görevler</h3>"
                     "<table cellpadding=\"6\" cellspacing=\"0\" style=\"border-collapse:collapse;font-size:13px\">"
                     f"<tr style=\"background:{h(color)};color:#fff\"><th align=\"left\">Görev</th>"
                     "<th align=\"left\">Sorumlu</th><th align=\"left\">Son tarih</th></tr>")
        for a in open_actions:
            parts.append(f"<tr style=\"border-bottom:1px solid #ddd\"><td>{h(a.text)}</td>"
                         f"<td>{h(a.owner or 'Belirlenmedi')}</td><td>{h(format_tr_short(a.due_date))}</td></tr>")
        parts.append("</table>")
    if meeting.next_meeting_date:
        parts.append(f"<p><b>Sonraki toplantı:</b> "
                     f"{h(format_date_time(meeting.next_meeting_date, meeting.next_meeting_time))}</p>")
    parts.append("<p style=\"color:#777;font-size:12px\">Bu e-posta J.A.R.V.I.S Toplantı Asistanı tarafından "
                 "otomatik olarak gönderilmiştir.</p></body></html>")
    return "".join(parts)


def build_minutes_message(meeting: Meeting, pdf_path: Path, sender: str, recipients: list[Recipient],
                          extra_attachments: list[Path] | None = None, test_mode: bool = False,
                          color: str = "#1F3A5F") -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject_for(meeting, test=test_mode)
    message["From"] = sender
    message["To"] = ", ".join(formataddr((r.name, r.email)) for r in recipients)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else None)
    message.set_content(_plain_body(meeting))
    message.add_alternative(_html_body(meeting, color), subtype="html")
    for path in [Path(pdf_path), *(extra_attachments or [])]:
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
    return message


# ---------------------------------------------------------------------- gönderim
class Mailer:
    def __init__(self, settings: Settings, outbox_dir: Path | None = None, dry_run: bool | None = None,
                 ssl_context: ssl.SSLContext | None = None):
        self.settings = settings
        self.dry_run = settings.mail_dry_run if dry_run is None else dry_run
        self.outbox_dir = outbox_dir or (settings.output_dir / "outbox")
        self._ssl_context = ssl_context  # testler için; normalde sistem sertifika deposu kullanılır

    @property
    def sender(self) -> str:
        return self.settings.smtp_from or self.settings.smtp_user or "jarvis@localhost"

    def check_config(self, require: bool = False) -> None:
        if self.dry_run and not require:
            return
        if not self.settings.smtp_host:
            raise MailConfigError("SMTP_HOST tanımlı değil. .env dosyasındaki SMTP ayarlarını doldurun "
                                  "veya test için MAIL_DRY_RUN=true kullanın.")
        if not (self.settings.smtp_from or self.settings.smtp_user):
            raise MailConfigError("SMTP_FROM (gönderen adres) tanımlı değil.")
        if self.settings.smtp_user and not self.settings.smtp_password:
            raise MailConfigError("SMTP_USER tanımlı ama SMTP_PASSWORD boş.")

    def send(self, message: EmailMessage, recipients: list[str]) -> SendResult:
        if self.dry_run:
            return self._write_eml(message, recipients)
        try:
            self.check_config()
        except MailConfigError as exc:
            return SendResult(False, "smtp", error=str(exc))
        stats: dict[str, str] = {}
        try:
            refused = self._smtp_send(message, recipients, stats)
        except SmtpStageError as failure:
            log.warning("SMTP hatası — aşama: %s, tür: %s, yanıt: %s", failure.stage,
                        type(failure.original).__name__, _safe_reply(failure.original))
            if isinstance(failure.original, smtplib.SMTPRecipientsRefused):
                refused = {addr: _reason(code, msg) for addr, (code, msg) in failure.original.recipients.items()}
                return SendResult(False, "smtp", refused=refused, error="Sunucu tüm alıcıları reddetti.",
                                  stage=failure.stage)
            return SendResult(False, "smtp", error=self.explain(failure), stage=failure.stage)
        sent = [r for r in recipients if r not in refused]
        log.info("SMTP: sunucu iletiyi kabul etti (%d alıcı, %d reddedildi): %s", len(sent), len(refused),
                 stats.get("accepted", ""))
        return SendResult(bool(sent) and not refused, "smtp", sent_to=sent, refused=refused,
                          error="" if not refused else "Bazı alıcılar reddedildi.",
                          accepted_reply=stats.get("accepted", ""))

    # ---------------------------------------------------------------- hata açıklaması
    def explain(self, failure: SmtpStageError) -> str:
        """Aşamaya ve SMTP koduna göre anlaşılır Türkçe açıklama (gizli bilgi içermez)."""
        exc, stage = failure.original, failure.stage
        where = f"[{stage}] "
        gmail = self._is_gmail
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return where + _auth_message(exc.smtp_code, _decode(exc.smtp_error), gmail)
        if isinstance(exc, UnicodeEncodeError):
            return where + ("SMTP_USER veya SMTP_PASSWORD Türkçe/ASCII dışı karakter içeriyor; SMTP girişi bunu "
                            "desteklemez. Gmail uygulama şifresi yalnızca 16 İngilizce harften oluşur.")
        if isinstance(exc, smtplib.SMTPNotSupportedError):
            if stage == Stage.STARTTLS:
                return where + "Sunucu STARTTLS desteklemiyor. 465 için SMTP_PORT=465 ve SMTP_USE_SSL=true deneyin."
            return where + ("Sunucu bu bağlantıda kimlik doğrulamayı (AUTH) ilan etmedi. 587 portunda "
                            "SMTP_USE_TLS=true olmalı (Gmail girişi yalnızca TLS sonrası kabul eder).")
        if isinstance(exc, ssl.SSLError):
            return where + (f"TLS/SSL hatası: {exc.__class__.__name__}. 587 için SMTP_USE_TLS=true ve "
                            "SMTP_USE_SSL=false; 465 için SMTP_USE_SSL=true olmalı. Antivirüsün e-posta taraması "
                            "TLS'i bozabilir.")
        if isinstance(exc, smtplib.SMTPSenderRefused):
            return where + (f"Gönderen adres reddedildi ({_reason(exc.smtp_code, exc.smtp_error)}). "
                            + ("Gmail'de SMTP_FROM, giriş yapılan hesap (SMTP_USER) olmalıdır." if gmail else ""))
        if isinstance(exc, smtplib.SMTPDataError):
            return where + (f"Sunucu iletiyi kabul etmedi ({_reason(exc.smtp_code, exc.smtp_error)}). "
                            "Ek boyutunu veya içerik politikasını kontrol edin.")
        if isinstance(exc, smtplib.SMTPServerDisconnected):
            return where + _disconnect_message(stage, gmail)
        if isinstance(exc, smtplib.SMTPResponseException):
            return where + f"SMTP hatası: {_reason(exc.smtp_code, exc.smtp_error)}"
        if isinstance(exc, (smtplib.SMTPConnectError, ConnectionError, socket.timeout, TimeoutError, OSError)):
            return where + self._unreachable(exc)
        return where + f"SMTP hatası: {exc.__class__.__name__}"

    @property
    def _is_gmail(self) -> bool:
        host = (self.settings.smtp_host or "").lower()
        return host.endswith("gmail.com") or host.endswith("googlemail.com")

    def _unreachable(self, exc: Exception) -> str:
        return (f"E-posta sunucusuna ulaşılamadı ({self.settings.smtp_host}:{self.settings.smtp_port}): "
                f"{exc.__class__.__name__}: {exc}. İnternet bağlantısını ve SMTP ayarlarını kontrol edin.")

    # ---------------------------------------------------------------- SMTP oturumu
    def _context(self) -> ssl.SSLContext:
        return self._ssl_context or ssl.create_default_context()

    def _password(self) -> str:
        """Gmail uygulama şifresi ekranda "abcd efgh ijkl mnop" diye gösterilir; boşluklar şifreye ait değildir."""
        password = self.settings.smtp_password or ""
        if self._is_gmail and _APP_PASSWORD_SPACED.match(password.strip()):
            return password.replace(" ", "").strip()
        return password

    def _open(self, steps: list[CheckStep] | None = None) -> smtplib.SMTP:
        """Bağlantı → EHLO → (STARTTLS → EHLO) → GİRİŞ. Her aşama izlenir; hata SmtpStageError olarak yükselir."""
        s = self.settings
        stage = Stage.CONNECT
        server: smtplib.SMTP | None = None
        try:
            if s.smtp_use_ssl:
                server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout, context=self._context())
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout)
            _step(steps, stage, f"{s.smtp_host}:{s.smtp_port}" + (" (SSL)" if s.smtp_use_ssl else ""))
            stage = Stage.EHLO
            _step(steps, stage, _code(server.ehlo()))
            if s.smtp_use_tls and not s.smtp_use_ssl:
                stage = Stage.STARTTLS
                _step(steps, stage, _code(server.starttls(context=self._context())))
                stage = Stage.EHLO_TLS
                _step(steps, stage, _code(server.ehlo()))
            if s.smtp_user:
                stage = Stage.LOGIN
                code = self._login(server)
                _step(steps, stage, f"{code} (yöntem: {self._auth_method(server)})")
            return server
        except Exception as exc:
            if server is not None:
                _quit(server)  # bağlantı her durumda kapatılır (QUIT, olmazsa doğrudan kapatma)
            raise SmtpStageError(stage, exc) from exc

    @staticmethod
    def _auth_method(server: smtplib.SMTP) -> str:
        features = getattr(server, "esmtp_features", None)
        advertised = features.get("auth", "").upper().split() if isinstance(features, dict) else []
        return "PLAIN" if "PLAIN" in advertised or not advertised else ("LOGIN" if "LOGIN" in advertised else
                                                                         advertised[0])

    def _login(self, server: smtplib.SMTP) -> int:
        """Tek yöntemle giriş: reddedilen girişten sonra ikinci yöntem denenmez (asıl 535/534 yanıtı korunur)."""
        features = getattr(server, "esmtp_features", None)
        if isinstance(features, dict) and features.get("auth"):
            features["auth"] = self._auth_method(server)  # smtplib.login yalnızca bu yöntemi dener
        result = server.login(self.settings.smtp_user, self._password())
        return result[0] if isinstance(result, tuple) and result else 235

    def _smtp_send(self, message: EmailMessage, recipients: list[str], stats: dict[str, str] | None = None
                   ) -> dict[str, str]:
        stats = stats if stats is not None else {}
        server = self._open()
        current = {"stage": Stage.MAIL_FROM}

        def track(name: str, method):
            def wrapper(*args, **kwargs):
                current["stage"] = name
                result = method(*args, **kwargs)
                if name == Stage.DATA and isinstance(result, tuple) and len(result) == 2:
                    stats["accepted"] = _reason(result[0], result[1])[:200]
                return result
            return wrapper
        # smtplib.send_message → sendmail → self.mail / self.rcpt / self.data: aşama bilgisi için sarılır
        for attr, name in (("mail", Stage.MAIL_FROM), ("rcpt", Stage.RCPT_TO), ("data", Stage.DATA)):
            original = getattr(server, attr, None)
            if callable(original):
                setattr(server, attr, track(name, original))
        try:
            refused = server.send_message(message, from_addr=self.sender, to_addrs=recipients)
        except Exception as exc:
            _quit(server)
            raise SmtpStageError(current["stage"], exc) from exc
        _quit(server)
        return {addr: _reason(code, msg) for addr, (code, msg) in (refused or {}).items()}

    def check_connection(self) -> CheckResult:
        """--smtp-check: yalnızca bağlantı, TLS ve kimlik doğrulama. MAIL FROM / RCPT / DATA GÖNDERİLMEZ;
        hiçbir e-posta gönderilmez."""
        steps: list[CheckStep] = []
        try:
            self.check_config(require=True)  # MAIL_DRY_RUN açık olsa da bağlantı denenir; gönderim yine yoktur
        except MailConfigError as exc:
            return CheckResult(False, steps, str(exc))
        try:
            server = self._open(steps)
        except SmtpStageError as failure:
            steps.append(CheckStep(failure.stage, False, _safe_reply(failure.original)))
            log.warning("SMTP kontrolü başarısız — aşama: %s, tür: %s, yanıt: %s", failure.stage,
                        type(failure.original).__name__, _safe_reply(failure.original))
            return CheckResult(False, steps, self.explain(failure))
        _quit(server)
        steps.append(CheckStep(Stage.QUIT, True, "e-posta gönderilmedi"))
        return CheckResult(True, steps)

    def _write_eml(self, message: EmailMessage, recipients: list[str]) -> SendResult:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        path = self.outbox_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{abs(hash(message['Subject'])) % 10000:04d}.eml"
        path.write_bytes(bytes(message))
        log.info("Dry-run: e-posta dosyaya yazıldı: %s", path)
        return SendResult(True, "dry-run", sent_to=list(recipients), eml_path=str(path))


def _reason(code: int, msg: bytes | str) -> str:
    text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
    return f"{code} {text}".strip()


def describe_settings(settings: Settings) -> list[str]:
    """--smtp-check için ayar özeti. Parola ve kullanıcı adı açık yazılmaz."""
    password = settings.smtp_password or ""
    host = (settings.smtp_host or "").lower()
    gmail = host.endswith("gmail.com") or host.endswith("googlemail.com")
    if not password:
        pw_note = "boş"
    elif gmail and re.fullmatch(r"[a-z]{16}", password.replace(" ", "")):
        pw_note = "ayarlı (Gmail uygulama şifresi biçiminde)"
    elif gmail:
        pw_note = "ayarlı (UYARI: Gmail uygulama şifresi biçiminde değil; 16 İngilizce harf olmalı)"
    else:
        pw_note = "ayarlı"
    lines = [
        f"SMTP_HOST={settings.smtp_host or '(boş)'}  SMTP_PORT={settings.smtp_port}  "
        f"SMTP_USE_TLS={settings.smtp_use_tls}  SMTP_USE_SSL={settings.smtp_use_ssl}  "
        f"SMTP_TIMEOUT={settings.smtp_timeout}",
        f"SMTP_USER={mask_address(settings.smtp_user)}  SMTP_FROM={mask_address(settings.smtp_from)}  "
        f"SMTP_PASSWORD: {pw_note}",
        f"MAIL_DRY_RUN={settings.mail_dry_run}  MAIL_REDIRECT_TO={mask_address(settings.mail_redirect_to)}  "
        f"MAIL_ALLOWED_DOMAINS={','.join(settings.mail_allowed_domains) or '(sınırsız)'}",
    ]
    if settings.smtp_port == 465 and not settings.smtp_use_ssl:
        lines.append("UYARI: 465 portu doğrudan SSL ister; SMTP_USE_SSL=true olmalı.")
    if settings.smtp_port == 587 and settings.smtp_use_ssl:
        lines.append("UYARI: 587 portu STARTTLS ister; SMTP_USE_SSL=false ve SMTP_USE_TLS=true olmalı.")
    if gmail and settings.smtp_from and settings.smtp_user and settings.smtp_from.lower() != settings.smtp_user.lower():
        lines.append("Not: Gmail, SMTP_FROM farklıysa gönderen adresi hesabın adresiyle değiştirebilir.")
    return lines


def mask_address(address: str) -> str:
    if not address:
        return "(boş)"
    if "@" not in address:
        return address[:1] + "***"
    local, domain = address.split("@", 1)
    return f"{local[:1]}***{local[-1:] if len(local) > 2 else ''}@{domain}"


# ---------------------------------------------------------------------- yardımcılar
_APP_PASSWORD_SPACED = re.compile(r"^[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}$")


def _step(steps: list[CheckStep] | None, stage: str, detail: str) -> None:
    if steps is not None:
        steps.append(CheckStep(stage, True, detail))


def _close(server: smtplib.SMTP) -> None:
    try:
        server.close()
    except Exception:
        pass


def _quit(server: smtplib.SMTP) -> None:
    try:
        server.quit()
    except smtplib.SMTPException:
        _close(server)
    except OSError:
        pass


def _code(reply: object) -> str:
    return str(reply[0]) if isinstance(reply, tuple) and reply else "tamam"


def _decode(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def _safe_reply(exc: BaseException) -> str:
    """Günlük için: yalnızca SMTP kodu ve sunucu yanıtının ilk satırı (kimlik bilgisi içermez)."""
    if isinstance(exc, smtplib.SMTPResponseException):
        first = _decode(exc.smtp_error).splitlines()[0] if exc.smtp_error else ""
        return f"{exc.smtp_code} {first}"[:160]
    if isinstance(exc, UnicodeEncodeError):
        return "ASCII dışı karakter"
    return f"{exc.__class__.__name__}: {exc}"[:160]


def _auth_message(code: int, reply: str, gmail: bool) -> str:
    enhanced = reply.split()[0] if reply else ""
    if code == 534 and "5.7.9" in reply:
        return ("Gmail normal hesap şifresini kabul etmiyor; uygulama şifresi gerekiyor. Google Hesabı → Güvenlik → "
                "2 Adımlı Doğrulama'yı açın, sonra 'Uygulama şifreleri' bölümünden 16 harfli şifre oluşturup "
                "SMTP_PASSWORD'e yazın.")
    if code == 534:
        return ("Gmail bu girişi engelledi (534). Tarayıcıda hesaba girip güvenlik uyarısını onaylayın, ardından "
                "uygulama şifresiyle yeniden deneyin.")
    if code == 535:
        hint = (" Gmail için: SMTP_USER tam Gmail adresi olmalı; SMTP_PASSWORD 16 harfli uygulama şifresi olmalı "
                "(normal hesap şifresi değil); 2 Adımlı Doğrulama açık olmalı; uygulama şifresi iptal edildiyse "
                "yenisini oluşturun.") if gmail else ""
        return f"Kullanıcı adı veya şifresi hatalı; sunucu kabul etmedi (535 {enhanced}).{hint}"
    if code == 454:
        return "Sunucu geçici olarak girişi reddetti (454; çok fazla deneme olabilir). Birkaç dakika bekleyin."
    return f"Kimlik doğrulama reddedildi; kullanıcı adı veya şifresi hatalı olabilir ({code} {enhanced})." + (
        " Gmail için uygulama şifresi gerekir." if gmail else "")


def _disconnect_message(stage: str, gmail: bool) -> str:
    if stage == Stage.LOGIN:
        return ("Kimlik doğrulama sırasında sunucu bağlantıyı kapattı. Sunucular genellikle reddedilen girişten "
                "sonra bağlantıyı keser" + ("; Gmail uygulama şifresini ve SMTP_USER'ı kontrol edin" if gmail else "")
                + ". Antivirüsün e-posta kalkanı da bağlantıyı kesebilir. 'python main.py --smtp-check' ile "
                "yeniden deneyin.")
    if stage in (Stage.MAIL_FROM, Stage.RCPT_TO, Stage.DATA):
        return (f"{stage} aşamasında sunucu bağlantıyı kapattı; ileti KABUL EDİLMEDİ. Olası nedenler: ek boyutu, "
                "gönderim sınırı, içerik reddi veya antivirüs e-posta taraması.")
    return (f"{stage} aşamasında sunucu bağlantıyı kapattı. SMTP_PORT / SMTP_USE_TLS / SMTP_USE_SSL ayarlarını ve "
            "antivirüs/güvenlik duvarı e-posta taramasını kontrol edin.")
