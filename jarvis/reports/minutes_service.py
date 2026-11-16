"""Tutanak iş akışı: PDF üretimi, e-posta gönderimi, gönderim durumu.

Kurallar (talimat Bölüm 9 ve 11):
  * PDF veya SMTP hatası toplantı kaydını asla silmez; toplantı önceden kaydedilmiştir.
  * Gönderim durumu `Meeting.email_status` içinde saklanır:
      sent_to   — tutanağın başarıyla ulaştığı adresler
      failed    — reddedilen adresler ve nedenleri
      attempts  — her denemenin kaydı (zaman, kip, alıcılar, sonuç)
  * Aynı adrese ikinci kez gönderilmez; "tutanağı tekrar gönder" (force) gerekir.
    Başarısız ya da kısmen başarılı bir gönderimden sonra "tutanağı gönder"
    yalnızca henüz ulaşmamış adreslere gönderir.
  * Dry-run ve test yönlendirmesi (MAIL_REDIRECT_TO) gerçek gönderim sayılmaz.
  * Toplantı bitmeden e-posta gönderilmez; ancak ara tutanak (PDF) üretilebilir.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from config.settings import Settings

from ..mail.mailer import Mailer, Recipient, build_minutes_message, collect_recipients
from ..meeting.manager import OperationResult
from ..meeting.models import Meeting, dt_to_str, now
from ..meeting.storage import MeetingRepository, StorageError
from ..utils.dates import MONTH_NAMES, format_tr_short
from ..utils.turkish_text import ascii_fold
from .fonts import FontError
from .excel_export import ExcelUnavailable, TaskExporter
from .ics import InviteDetails, build_ics, write_atomic
from .pdf_generator import CarriedAction, MinutesContext, MinutesPdfGenerator, PdfError

log = logging.getLogger(__name__)

MAX_ATTEMPTS_KEPT = 20


class EmailStatus:
    SENT = "gönderildi"
    PARTIAL = "kısmen gönderildi"
    FAILED = "gönderilemedi"
    DRY_RUN = "taslak (gönderilmedi)"


def slugify(text: str, limit: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_fold(text)).strip("-")
    return slug[:limit].strip("-") or "toplanti"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MinutesService:
    def __init__(self, repository: MeetingRepository, settings: Settings,
                 generator: MinutesPdfGenerator | None = None, mailer: Mailer | None = None,
                 clock: Callable[[], datetime] = now, demo: bool = False):
        self.repo = repository
        self.settings = settings
        self.demo = demo
        self.clock = clock
        self.generator = generator or MinutesPdfGenerator(
            company_name=settings.company_name, logo_path=settings.logo_path,
            primary_color=settings.pdf_primary_color, font_regular=settings.pdf_font_regular,
            font_bold=settings.pdf_font_bold, accent_color=settings.pdf_accent_color)
        # Demo modunda gerçek SMTP asla kullanılmaz.
        self.mailer = mailer or Mailer(settings, dry_run=True if demo else None)

    # ================================================================ PDF
    def pdf_path_for(self, meeting: Meeting, interim: bool = False) -> Path:
        name = f"{meeting.started_at:%Y-%m-%d_%H%M}_{slugify(meeting.title)}_{meeting.id[-6:]}"
        if interim:
            name += "_ARA-TUTANAK"
        return self.settings.output_dir / f"{name}.pdf"

    def build_context(self, meeting: Meeting, interim: bool = False) -> MinutesContext:
        as_of = self.clock().replace(microsecond=0)
        open_actions = self.repo.get_open_actions(exclude_meeting_id=meeting.id, include_demo=meeting.is_demo)
        closed_actions = self.repo.get_actions(meeting.carryover_completed)
        headers = self.repo.get_meeting_headers(
            [a.meeting_id for a in open_actions + closed_actions] + [meeting.previous_meeting_id or ""])

        def carried(actions) -> list[CarriedAction]:
            items = []
            for action in actions:
                header = headers.get(action.meeting_id, {})
                started = header.get("started_at") or ""
                date_text = format_tr_short(datetime.fromisoformat(started).date()) if started else ""
                items.append(CarriedAction(action, header.get("title", ""), date_text))
            return items

        previous = headers.get(meeting.previous_meeting_id or "", {})
        previous_date = previous.get("started_at")
        return MinutesContext(
            meeting=meeting, as_of=as_of, interim=interim,
            carryover_open=carried(open_actions), carryover_closed=carried(closed_actions),
            previous_title=previous.get("title", ""),
            previous_date=format_tr_short(datetime.fromisoformat(previous_date).date()) if previous_date else "",
        )

    def create_pdf(self, meeting: Meeting, interim: bool | None = None) -> OperationResult:
        """Tutanak PDF'ini üretir. Aktif toplantı için otomatik olarak ara tutanak üretilir."""
        interim = meeting.is_active if interim is None else interim
        try:
            context = self.build_context(meeting, interim=interim)
            path = self.generator.generate(context, self.pdf_path_for(meeting, interim))
        except (PdfError, FontError, OSError, StorageError) as exc:
            log.exception("Tutanak PDF'i oluşturulamadı")
            return OperationResult(False, f"Tutanak PDF'i oluşturulamadı: {exc}. Toplantı verisi kayıtlı; "
                                          "sorunu giderip 'Jarvis, tutanak oluştur' ile tekrar deneyebilirsiniz.")
        warnings = list(self.generator.warnings)
        if not interim:
            meeting.minutes_pdf = str(path)
            self._save(meeting)
        kind = "Ara tutanak" if interim else "Tutanak"
        message = f"{kind} oluşturuldu: {self._display_path(path)}."
        if warnings:
            message += " Uyarı: " + " ".join(warnings)
        return OperationResult(True, message, {"path": str(path), "interim": interim, "warnings": warnings})

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.settings.base_dir))
        except ValueError:
            return str(path)

    def _save(self, meeting: Meeting) -> None:
        try:
            self.repo.save(meeting)
        except StorageError:
            log.exception("Tutanak durumu kaydedilemedi")

    # ================================================================ e-posta
    def send_minutes(self, meeting: Meeting, force: bool = False) -> OperationResult:
        if meeting.is_active:
            return OperationResult(False, "Toplantı bitmeden tutanak gönderilmez. Önce 'toplantıyı bitir' deyin.")
        pdf = Path(meeting.minutes_pdf) if meeting.minutes_pdf else None
        if pdf is None or not pdf.is_file():
            created = self.create_pdf(meeting, interim=False)
            if not created.ok:
                return created
            pdf = Path(created.data["path"])

        recipient_list = collect_recipients(meeting, self.settings)
        test_mode = bool(self.settings.mail_redirect_to)
        if test_mode:
            targets = [Recipient("Test alıcısı", self.settings.mail_redirect_to)]
        else:
            targets = recipient_list.recipients
        skipped_note = ""
        if recipient_list.skipped:
            skipped_note = " Gönderilmeyen: " + "; ".join(f"{n} ({r})" for n, r in recipient_list.skipped) + "."
        if not targets:
            return OperationResult(False, "Tutanak gönderilecek geçerli e-posta adresi yok." + skipped_note)

        status = meeting.email_status
        already = {e.lower() for e in status.get("sent_to", [])}
        if not force and not test_mode and not self.mailer.dry_run:
            pending = [r for r in targets if r.email.lower() not in already]
            if not pending:
                return OperationResult(False, f"Tutanak bu {len(targets)} alıcıya zaten gönderildi. "
                                              "Yeniden göndermek için 'Jarvis, tutanağı tekrar gönder' deyin.",
                                       {"status": status.get("status")})
            targets = pending

        invite = self.invite_attachment(meeting)
        message = build_minutes_message(meeting, pdf, self.mailer.sender, targets, test_mode=test_mode,
                                        color=self.settings.pdf_primary_color,
                                        extra_attachments=[invite] if invite else None)
        result = self.mailer.send(message, [r.email for r in targets])
        self._record_attempt(meeting, pdf, targets, result, test_mode)
        self._save(meeting)
        return self._send_result_message(meeting, result, targets, test_mode, skipped_note)

    def _record_attempt(self, meeting: Meeting, pdf: Path, targets: list[Recipient], result, test_mode: bool) -> None:
        status = meeting.email_status
        mode = "test" if test_mode and result.mode == "smtp" else result.mode
        attempts = list(status.get("attempts", []))
        attempts.append({
            "at": dt_to_str(self.clock().replace(microsecond=0)), "mode": mode, "ok": result.ok,
            "recipients": [r.email for r in targets], "sent_to": result.sent_to, "refused": result.refused,
            "error": result.error, "eml": result.eml_path,
            # tanılama: hata aşaması ve sunucunun kabul yanıtı (gizli bilgi içermez)
            **({"stage": result.stage} if getattr(result, "stage", "") else {}),
            **({"accepted": result.accepted_reply} if getattr(result, "accepted_reply", "") else {}),
        })
        status["attempts"] = attempts[-MAX_ATTEMPTS_KEPT:]
        status["pdf"] = str(pdf)
        try:
            status["pdf_sha256"] = file_sha256(pdf)
        except OSError:
            pass
        if mode in ("dry-run", "test"):
            status.setdefault("status", EmailStatus.DRY_RUN)
            return
        sent = list(dict.fromkeys([*status.get("sent_to", []), *result.sent_to]))
        failed = {k: v for k, v in status.get("failed", {}).items() if k not in result.sent_to}
        failed.update(result.refused)
        if not result.ok and not result.refused and not result.sent_to:
            for recipient in targets:
                failed[recipient.email] = result.error
        status["sent_to"] = sent
        status["failed"] = failed
        expected = {r.email.lower() for r in collect_recipients(meeting, self.settings).recipients}
        delivered = {e.lower() for e in sent}
        if expected and expected <= delivered:
            status["status"] = EmailStatus.SENT
        elif delivered:
            status["status"] = EmailStatus.PARTIAL
        else:
            status["status"] = EmailStatus.FAILED

    def _send_result_message(self, meeting: Meeting, result, targets: list[Recipient], test_mode: bool,
                             skipped_note: str) -> OperationResult:
        data = {"status": meeting.email_status.get("status"), "sent_to": result.sent_to,
                "refused": result.refused, "mode": result.mode, "eml": result.eml_path}
        if result.mode == "dry-run":
            return OperationResult(True, f"Deneme kipi: e-posta gönderilmedi, {len(targets)} alıcılı taslak "
                                         f"kaydedildi: {self._display_path(Path(result.eml_path))}.{skipped_note}",
                                   data)
        # SMTP 250 yanıtı yalnızca sunucunun iletiyi KABUL ettiğini gösterir; gelen kutusuna ulaşma ayrıca doğrulanır
        inbox_note = " Ulaştığını alıcının gelen kutusundan (gerekirse spam klasöründen) doğrulayın."
        if test_mode and result.ok:
            return OperationResult(True, f"Test e-postası yalnızca {self.settings.mail_redirect_to} adresine "
                                         f"gönderildi; sunucu kabul etti.{inbox_note}", data)
        if result.ok:
            return OperationResult(True, f"Tutanak {len(result.sent_to)} alıcıya e-postayla gönderildi; sunucu "
                                         f"kabul etti.{inbox_note}{skipped_note}", data)
        if result.sent_to:
            refused = ", ".join(result.refused)
            return OperationResult(False, f"Tutanak {len(result.sent_to)} alıcıya gönderildi; şu adresler reddetti: "
                                          f"{refused}. Kalanlara yeniden göndermek için 'Jarvis, tutanağı gönder' "
                                          "deyin.", data)
        return OperationResult(False, f"E-posta gönderilemedi: {result.error} PDF saklandı; sorunu giderip "
                                      "'Jarvis, tutanağı gönder' ile yeniden deneyin.", data)

    # ================================================================ takvim daveti (Faz 6)
    def invite_path_for(self, meeting: Meeting) -> Path:
        return self.settings.output_dir / "davetler" / f"{meeting.id}_sonraki_toplanti.ics"

    def create_invite(self, meeting: Meeting) -> OperationResult:
        """Sonraki toplantının .ics davetini oluşturur/günceller. Saat yoksa oluşturmaz (saat uydurulmaz).
        Aynı içerik için dosya yeniden yazılmaz; değişiklikte aynı UID ile SEQUENCE artar (kopya etkinlik olmaz)."""
        if meeting.next_meeting_date is None:
            return OperationResult(False, "Sonraki toplantı tarihi kaydedilmemiş. Örnek: 'Jarvis, sonraki toplantı "
                                          "21 Ekim saat 14.00'.")
        if not meeting.next_meeting_time:
            return OperationResult(False, "Sonraki toplantının saati belirtilmediği için takvim daveti oluşturulmadı. "
                                          f"Örnek: 'Jarvis, sonraki toplantı {meeting.next_meeting_date.day} "
                                          f"{MONTH_NAMES[meeting.next_meeting_date.month].capitalize()} saat 14.00'.")
        details = InviteDetails(
            uid=f"{meeting.id}-sonraki-toplanti@jarvis.local", title=f"{meeting.title} — sonraki toplantı",
            day=meeting.next_meeting_date, hhmm=meeting.next_meeting_time,
            duration_min=self.settings.ics_duration_min, location=meeting.location,
            description=(f"{format_tr_short(meeting.started_at.date())} tarihli \"{meeting.title}\" toplantısında "
                         "kararlaştırıldı. J.A.R.V.I.S Toplantı Asistanı tarafından oluşturuldu."))
        path = self.invite_path_for(meeting)
        info = dict(meeting.next_meeting_invite)
        fingerprint = details.fingerprint()
        if info.get("fingerprint") == fingerprint and path.is_file():
            info["attach"] = True
            meeting.next_meeting_invite = info
            self._save(meeting)
            return OperationResult(True, "Takvim daveti zaten güncel.", {"path": str(path), "created": False})
        sequence = int(info.get("sequence", -1)) + 1 if info.get("uid") == details.uid else 0
        try:
            write_atomic(path, build_ics(details, sequence))
        except OSError as exc:
            log.exception("Takvim daveti yazılamadı")
            return OperationResult(False, f"Takvim daveti yazılamadı: {exc}. Toplantı kaydı etkilenmedi.")
        meeting.next_meeting_invite = {"path": str(path), "uid": details.uid, "sequence": sequence,
                                       "fingerprint": fingerprint, "attach": True,
                                       "created_at": dt_to_str(self.clock().replace(microsecond=0))}
        self._save(meeting)
        verb = "güncellendi" if sequence else "oluşturuldu"
        return OperationResult(True, f"Takvim daveti {verb}: {self._display_path(path)}.",
                               {"path": str(path), "created": True, "sequence": sequence})

    def invite_attachment(self, meeting: Meeting) -> Path | None:
        """E-postaya eklenecek geçerli davet dosyası (ayar açık, davet güncel ve dosya duruyorsa)."""
        info = meeting.next_meeting_invite or {}
        if not self.settings.mail_attach_ics or not info.get("attach") or not meeting.next_meeting_time:
            return None
        path = Path(info.get("path", ""))
        return path if path.is_file() else None

    def attach_invite(self, meeting: Meeting) -> OperationResult:
        """"Toplantı davetini e-postaya ekle": daveti hazırlar ve tutanak e-postasına eklenmek üzere işaretler.
        E-posta GÖNDERMEZ; gönderim yine yalnızca 'tutanağı gönder' ile ve mevcut güvenlik kurallarıyla yapılır."""
        created = self.create_invite(meeting)
        if not created.ok:
            return created
        if not self.settings.mail_attach_ics:
            return OperationResult(False, f"{created.message} Ancak MAIL_ATTACH_ICS=false; davet e-postaya eklenmez.")
        return OperationResult(True, f"{created.message} Davet, tutanak e-postasına PDF ile birlikte eklenecek. "
                                     "E-posta yalnızca 'Jarvis, tutanağı gönder' ile gönderilir.", created.data)

    # ================================================================ Excel görev listesi (Faz 6)
    def export_tasks(self, meeting: Meeting | None, scope: str = "meeting") -> OperationResult:
        """Görevleri .xlsx dosyasına aktarır. Yalnızca okur; toplantı kaydı ve SQLite değişmez."""
        exporter = TaskExporter(self.repo, self.settings.output_dir, clock=self.clock)
        if scope == "open":
            rows = exporter.open_rows(meeting if meeting is not None and meeting.is_active else None,
                                      include_demo=self.demo)
            label, hint = "Tüm açık görevler", "acik"
        else:
            if meeting is None:
                return OperationResult(False, "Görevleri aktarılacak toplantı bulunamadı.")
            rows = exporter.meeting_rows(meeting)
            label = f"{meeting.title} ({format_tr_short(meeting.started_at.date())}) görevleri ve devreden görevler"
            hint = f"{slugify(meeting.title, 30)}_{meeting.id[-6:]}"
        if not rows:
            return OperationResult(False, "Aktarılacak görev yok; Excel dosyası oluşturulmadı.")
        try:
            path = exporter.export(rows, label, hint)
        except ExcelUnavailable as exc:
            return OperationResult(False, str(exc))
        except OSError as exc:
            log.exception("Excel dosyası yazılamadı")
            return OperationResult(False, f"Excel dosyası yazılamadı: {exc}. Dosya açıksa kapatıp tekrar deneyin.")
        what = "Açık görev listesi" if scope == "open" else "Görev listesi"
        return OperationResult(True, f"{what} Excel dosyasına aktarıldı ({len(rows)} görev): "
                                     f"{self._display_path(path)}.", {"path": str(path), "rows": len(rows)})

    # ================================================================ akışlar
    def finalize(self, meeting: Meeting) -> OperationResult:
        """Toplantı bitişinde: PDF üret, ayar açıksa e-postayı gönder."""
        pdf = self.create_pdf(meeting, interim=False)
        if not pdf.ok:
            return pdf
        if self.settings.auto_send_minutes and not self.demo:
            sent = self.send_minutes(meeting)
            return OperationResult(sent.ok, f"{pdf.message} {sent.message}", {**pdf.data, "email": sent.data})
        hint = " Göndermek için 'Jarvis, tutanağı gönder' deyin."
        return OperationResult(True, pdf.message + hint, pdf.data)
