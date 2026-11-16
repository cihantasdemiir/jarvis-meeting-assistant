"""Türkçe kurumsal toplantı tutanağı (PDF, G-10).

ReportLab platypus ile A4 tutanak üretir. Bölümler ("01 | Başlık" biçiminde numaralı):
  Künye, Yönetici özeti, Katılım, Gündem, Görüşme notları, Kararlar, Görevler,
  Devreden işler, Oylamalar, Otopark, Konuşmacı istatistikleri, Sonraki toplantı,
  Ek A Transkript (yalnızca transkript kaydı varsa; yoksa ek ve sayfası oluşturulmaz).
Boş bölümler atlanır; numaralandırma kesintisiz kalır. Uzun tablolar sayfalar arasında bölünür ve başlık satırı
tekrar edilir.

Kurumsal görünüm (yapılandırılabilir şablon): her sayfada üstte logo / şirket adı ve
"TOPLANTI TUTANAĞI", altta şirket unvanı ve "Sayfa X / Y". Logo yalnızca LOGO_PATH ile verilen resmî dosyadan
basılır; dosya yoksa logo alanı metinle işaretlenir, logo çizilmez veya taklit edilmez.

Resmî tutanak yalnızca kayıtlı (onaylanmış) toplantı verisini basar: LLM taslakları (`Meeting.ai_drafts`) onaylanıp
karar/görev kaydına dönüşmedikçe PDF'e girmez.

Bu modül yalnızca çizim yapar; hangi verinin basılacağına MinutesService karar verir.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..meeting.models import ActionItem, AgendaStatus, Meeting, TranscriptKind
from ..meeting.participant_ops import removed_label
from ..meeting.speaker_tracker import SpeakerTracker
from ..meeting.summarizer import MeetingSummarizer
from ..utils.dates import format_date_time, format_tr_date, format_tr_short
from ..utils.turkish_text import tr_upper
from .fonts import FontSet, register_turkish_fonts

# Konuşmacı istatistiği dipnotları: anlamı değiştirilmeden iki ayrı paragraf olarak basılır.
SPEAKER_NOTES = (
    "* Söz süresi: «söz X'te» komutundan söz başkasına geçene veya kapatılana kadar geçen süredir; kişinin "
    "gerçekten konuştuğu süre değildir (sessizlik ve asistanın yanıtları dahildir, program kapalıyken geçen süre "
    "sayılmaz).",
    "** Kayda geçen konuşma: o kişi adına transkripte yazılan konuşma bölümleri; sesli girişte bu bölümlerin ses "
    "süresi. Komutlar ve asistanın sesi dahil değildir.",
)
SPEAKER_NOTE = " ".join(SPEAKER_NOTES)
TRANSCRIPT_COMMAND_NOTE = ("Gri ve köşeli parantezle işaretli satırlar «Jarvis» hitabı duyulmadan söylenmiş komut "
                           "kalıplarıdır. Kayıt için korunmuştur; toplantı konuşması, karar/görev kaynağı veya "
                           "konuşma istatistiği olarak sayılmaz.")

log = logging.getLogger(__name__)

# Görünüm tokenları — RESMÎ KURUMSAL PALET DEĞİL (kurumun marka kılavuzuna göre .env ile değiştirilir)
DEFAULT_PRIMARY = "#143448"  # lacivert: başlıklar, tablo başlığı
DEFAULT_ACCENT = "#197E89"  # petrol: ince vurgu
TEXT_COLOR = "#233744"
MUTED_COLOR = "#72828A"
LIGHT_FILL = "#F1F5F6"
LINE_COLOR = "#DCE5E8"
BADGE_COLOR = "#B00020"
LOGO_PLACEHOLDER = "Logo alanı — resmî logo dosyası tanımlanmadı"

# Sayfa ölçüleri (mm)
MARGIN_MM = 15
TOP_MARGIN_MM = 34
BOTTOM_MARGIN_MM = 24
LOGO_BOX_MM = (46, 14)  # en × boy; logo oranı korunarak bu kutuya sığdırılır


@dataclass
class CarriedAction:
    """Önceki bir toplantıdan gelen görev ve kaynağı."""

    action: ActionItem
    source_title: str = ""
    source_date: str = ""


@dataclass
class MinutesContext:
    """PDF'e basılacak verinin anlık görüntüsü."""

    meeting: Meeting
    as_of: datetime
    interim: bool = False  # toplantı bitmeden üretilen ara tutanak
    carryover_open: list[CarriedAction] = field(default_factory=list)
    carryover_closed: list[CarriedAction] = field(default_factory=list)
    previous_title: str = ""
    previous_date: str = ""


class PdfError(RuntimeError):
    """PDF üretilemediğinde fırlatılır."""


def _hex_color(value: str, fallback: str = "#1F3A5F"):
    from reportlab.lib import colors  # noqa: PLC0415

    try:
        return colors.HexColor(value if value.startswith("#") else f"#{value}")
    except (ValueError, AttributeError):
        return colors.HexColor(fallback)


def _p(text: object) -> str:
    """Paragraf içine güvenle konacak metin (XML kaçışlı, satır sonları korunur)."""
    return escape(str(text if text is not None else "")).replace("\n", "<br/>")


def _tr_cap(text: str) -> str:
    """İlk harfi Türkçe kurala göre büyütür ("iptal" -> "İptal")."""
    return tr_upper(text[:1]) + text[1:] if text else text


def _duration_text(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} dk"
    return f"{minutes // 60} sa {minutes % 60} dk"


class MinutesPdfGenerator:
    def __init__(self, company_name: str = "", logo_path: Path | None = None, primary_color: str = DEFAULT_PRIMARY,
                 font_regular: Path | None = None, font_bold: Path | None = None,
                 accent_color: str = DEFAULT_ACCENT):
        self.company_name = company_name
        self.logo_path = logo_path
        self.primary_hex = primary_color or DEFAULT_PRIMARY
        self.accent_hex = accent_color or DEFAULT_ACCENT
        self._font_paths = (font_regular, font_bold)
        self.warnings: list[str] = []
        self._frame_width_mm = 210.0 - 2 * MARGIN_MM

    # ================================================================ giriş
    def generate(self, context: MinutesContext, output_path: Path) -> Path:
        """PDF'i önce geçici dosyaya yazar, sonra hedefe taşır (yarım dosya kalmaz)."""
        from reportlab.lib.pagesizes import A4  # noqa: PLC0415
        from reportlab.lib.units import mm  # noqa: PLC0415
        from reportlab.platypus import SimpleDocTemplate  # noqa: PLC0415

        self.warnings = []
        fonts = register_turkish_fonts(*self._font_paths)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._frame_width_mm = A4[0] / mm - 2 * MARGIN_MM
        logo = self._load_logo()
        story = self._build_story(context, fonts)

        handle, temp_name = tempfile.mkstemp(suffix=".pdf", dir=str(output_path.parent))
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            doc = SimpleDocTemplate(
                str(temp_path), pagesize=A4, leftMargin=MARGIN_MM * mm, rightMargin=MARGIN_MM * mm,
                topMargin=TOP_MARGIN_MM * mm, bottomMargin=BOTTOM_MARGIN_MM * mm,
                title=f"{context.meeting.title} — Toplantı Tutanağı", author="J.A.R.V.I.S",
                subject="Toplantı Tutanağı", creator="J.A.R.V.I.S Toplantı Asistanı",
            )
            decorate = self._page_decorator(context, fonts, logo)
            doc.build(story, onFirstPage=decorate, onLaterPages=decorate,
                      canvasmaker=_numbered_canvas_factory(fonts.regular, MUTED_COLOR))
            return _move_into_place(temp_path, output_path)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            if isinstance(exc, PdfError):
                raise
            raise PdfError(f"PDF oluşturulamadı: {exc}") from exc

    # ================================================================ stiller
    def _styles(self, fonts: FontSet) -> dict:
        from reportlab.lib import colors  # noqa: PLC0415
        from reportlab.lib.styles import ParagraphStyle  # noqa: PLC0415

        primary = _hex_color(self.primary_hex, DEFAULT_PRIMARY)
        accent = _hex_color(self.accent_hex, DEFAULT_ACCENT)
        text = colors.HexColor(TEXT_COLOR)
        muted = colors.HexColor(MUTED_COLOR)
        base = ParagraphStyle("base", fontName=fonts.regular, fontSize=9, leading=13.5, textColor=text)
        return {
            "base": base,
            "small": ParagraphStyle("small", parent=base, fontSize=7.4, leading=10, textColor=muted),
            # dipnot: tablo altındaki açıklamalar; küçük ama rahat okunur
            "note": ParagraphStyle("note", parent=base, fontSize=8.2, leading=12, textColor=text, spaceBefore=3,
                                   spaceAfter=2),
            "cell": ParagraphStyle("cell", parent=base, fontSize=8, leading=11),
            "cell_bold": ParagraphStyle("cell_bold", parent=base, fontName=fonts.bold, fontSize=8, leading=11,
                                        textColor=primary),
            "head": ParagraphStyle("head", parent=base, fontName=fonts.bold, fontSize=7.6, leading=10.5,
                                   textColor=colors.white),
            "key": ParagraphStyle("key", parent=base, fontName=fonts.bold, fontSize=7.2, leading=10.5,
                                  textColor=muted),
            "value": ParagraphStyle("value", parent=base, fontSize=8.6, leading=11.5, textColor=primary),
            "title": ParagraphStyle("title", parent=base, fontName=fonts.bold, fontSize=18, leading=23,
                                    textColor=primary),
            "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=9.5, leading=13, textColor=muted),
            "h2": ParagraphStyle("h2", parent=base, fontName=fonts.bold, fontSize=10.5, leading=14,
                                 textColor=primary, spaceBefore=14, spaceAfter=7, keepWithNext=1),
            "h3": ParagraphStyle("h3", parent=base, fontName=fonts.bold, fontSize=8.8, leading=12,
                                 textColor=primary, spaceBefore=6, spaceAfter=4, keepWithNext=1),
            "badge": ParagraphStyle("badge", parent=base, fontName=fonts.bold, fontSize=8.5, leading=11,
                                    textColor=colors.HexColor(BADGE_COLOR)),
            "primary": primary,
            "accent": accent,
            "light": colors.HexColor(LIGHT_FILL),
            "line": colors.HexColor(LINE_COLOR),
        }

    # ================================================================ içerik
    def _build_story(self, ctx: MinutesContext, fonts: FontSet) -> list:
        from reportlab.platypus import PageBreak, Paragraph, Spacer  # noqa: PLC0415

        st = self._styles(fonts)
        story: list = []
        story.extend(self._title_block(ctx, st))
        story.append(Spacer(1, 10))
        story.append(self._info_table(ctx, st))

        number = 0

        def heading(title: str) -> None:
            nonlocal number
            number += 1
            accent = self.accent_hex if self.accent_hex.startswith("#") else f"#{self.accent_hex}"
            story.append(Paragraph(f'<font color="{_p(accent)}">{number:02d}</font> | {_p(title)}', st["h2"]))

        summary = self._summary_text(ctx)
        if summary:
            heading("Yönetici Özeti")
            story.append(Paragraph(_p(summary), st["base"]))

        for title, builder in (
            ("Katılım", self._attendance),
            ("Gündem", self._agenda),
            ("Görüşme Notları", self._notes),
            ("Alınan Kararlar", self._decisions),
            ("Görevler", self._actions),
            ("Devreden İşler", self._carryover),
            ("Oylamalar", self._votes),
            ("Otopark (Gündem Dışı Konular)", self._parking),
            ("Konuşmacı İstatistikleri", self._speakers),
        ):
            flowables = builder(ctx, st)
            if not flowables:
                continue  # verisi olmayan bölüm boş tabloyla yer kaplamaz
            heading(title)
            story.extend(flowables)

        if ctx.meeting.next_meeting_date:
            heading("Sonraki Toplantı")
            when = format_date_time(ctx.meeting.next_meeting_date, ctx.meeting.next_meeting_time)
            story.append(Paragraph(_p(when), st["base"]))  # saat kayıtlı değilse uydurulmaz

        if not ctx.meeting.transcript:
            return story  # transkript kaydı yoksa Ek A ve boş sayfası hiç oluşturulmaz
        story.append(PageBreak())
        story.append(Paragraph("Ek A — Toplantı Transkripti", st["title"]))
        story.append(Paragraph(_p(f"{ctx.meeting.title} · {format_tr_date(ctx.meeting.started_at.date())}"),
                               st["subtitle"]))
        story.append(Spacer(1, 8))
        story.extend(self._transcript(ctx, st))
        return story

    def _title_block(self, ctx: MinutesContext, st: dict) -> list:
        from reportlab.platypus import Paragraph  # noqa: PLC0415

        texts = [Paragraph(_p(ctx.meeting.title), st["title"]), Paragraph("Toplantı Tutanağı", st["subtitle"])]
        badges = []
        if ctx.meeting.is_demo:
            badges.append("DEMO VERİSİ — gerçek toplantı kaydı değildir")
        if ctx.interim:
            badges.append(f"ARA TUTANAK — toplantı tamamlanmadı ({ctx.as_of:%d.%m.%Y %H:%M} itibarıyla)")
        texts.extend(Paragraph(_p(badge), st["badge"]) for badge in badges)
        return texts

    def _load_logo(self):
        """Resmî logo dosyasını okur (PNG/JPG/WEBP). Yoksa None: başlıkta logo alanı metinle işaretlenir."""
        if not self.logo_path:
            return None
        path = Path(self.logo_path)
        if not path.is_file():
            self.warnings.append(f"Logo dosyası bulunamadı: {path}")
            log.warning(self.warnings[-1])
            return None
        try:
            from reportlab.lib.utils import ImageReader  # noqa: PLC0415

            reader = ImageReader(str(path))
            width, height = reader.getSize()
            if width <= 0 or height <= 0:
                raise ValueError("geçersiz boyut")
            return reader
        except Exception as exc:
            self.warnings.append(f"Logo okunamadı ({path.name}): {exc}")
            log.warning(self.warnings[-1])
            return None

    def _info_table(self, ctx: MinutesContext, st: dict):
        m = ctx.meeting
        end = m.ended_at or (None if not ctx.interim else ctx.as_of)
        time_text = f"{m.started_at:%H:%M}"
        if end:
            time_text += f" – {end:%H:%M}"
        if ctx.interim:
            time_text += " (devam ediyor)"
        duration = _duration_text(((end or ctx.as_of) - m.started_at).total_seconds())
        present = len(m.present_participants)
        rows = [
            ("Tarih", format_tr_date(m.started_at.date())),
            ("Saat", time_text),
            ("Süre", duration),
            ("Toplantı başkanı", m.chair or "—"),
            ("Yer", m.location or "—"),
            ("Katılım", f"{present} / {len(m.participants)} kişi" if m.participants else "Katılımcı listesi yok"),
        ]
        if ctx.previous_title:
            rows.append(("Önceki toplantı", f"{ctx.previous_title} ({ctx.previous_date})"))
        rows.append(("Tutanak no", m.id))
        return self._key_value_table(rows, st)

    def _summary_text(self, ctx: MinutesContext) -> str:
        if ctx.meeting.summary and not ctx.interim:
            return ctx.meeting.summary
        tracker = SpeakerTracker(ctx.meeting)
        return MeetingSummarizer().closing_summary(
            ctx.meeting, tracker.stats(now=ctx.as_of), tracker.silent_participants(), len(ctx.carryover_open))

    # ---------------------------------------------------------------- bölümler
    def _attendance(self, ctx: MinutesContext, st: dict) -> list:
        from reportlab.platypus import Paragraph  # noqa: PLC0415

        participants = ctx.meeting.participants
        if not participants:
            return []
        rows = []
        for index, p in enumerate(participants, start=1):
            status = "Katıldı" if p.present else "Katılmadı"
            if p.added_during_meeting:
                status += " (listede yoktu)"
            name = f"{p.name} (ilk kayıt: {p.former_name})" if p.former_name else p.name
            rows.append([str(index), name, p.role or "—", status])
        flowables: list = [self._table(["#", "Ad Soyad", "Görev / Rol", "Katılım"], rows, st, [8, 60, 55, 50])]
        removed = ctx.meeting.removed_participants
        if removed:  # izlenebilirlik: düzeltilen kayıtlar tutanakta görünür, sessizce kaybolmaz
            listed = ", ".join(removed_label(p) for p in removed)
            flowables.append(Paragraph(_p(f"Toplantı sırasında listeden çıkarılan kayıtlar: {listed}."), st["small"]))
        return flowables

    def _agenda(self, ctx: MinutesContext, st: dict) -> list:
        items = ctx.meeting.agenda
        if not items:
            return []
        rows = []
        for index, item in enumerate(items, start=1):
            elapsed = item.elapsed_seconds
            if item.status == AgendaStatus.ACTIVE and item.started_at:
                elapsed += max(0.0, (ctx.as_of - item.started_at).total_seconds())
            actual = _duration_text(elapsed) if elapsed > 0 else "—"
            rows.append([str(index), item.title, item.presenter or "—", f"{item.planned_minutes} dk", actual,
                         _tr_cap(item.status)])
        return [self._table(["#", "Gündem maddesi", "Sunan", "Plan", "Gerçekleşen", "Durum"], rows, st,
                            [8, 60, 36, 16, 26, 28])]

    def _notes(self, ctx: MinutesContext, st: dict) -> list:
        from reportlab.platypus import Paragraph  # noqa: PLC0415

        flowables: list = []
        for item in ctx.meeting.agenda:
            if item.notes:
                flowables.append(Paragraph(_p(item.title), st["h3"]))
                flowables.extend(Paragraph(f"• {_p(note)}", st["base"]) for note in item.notes)
        if ctx.meeting.notes:
            flowables.append(Paragraph("Genel notlar", st["h3"]))
            flowables.extend(Paragraph(f"• {_p(note)}", st["base"]) for note in ctx.meeting.notes)
        return flowables

    def _decisions(self, ctx: MinutesContext, st: dict) -> list:
        decisions = ctx.meeting.decisions
        if not decisions:
            return []
        rows = [[str(i), d.text, d.agenda_item or "—"] for i, d in enumerate(decisions, start=1)]
        return [self._table(["#", "Karar", "Gündem maddesi"], rows, st, [8, 120, 46])]

    def _action_row(self, index: int, action: ActionItem, extra: str | None = None) -> list[str]:
        row = [str(index), action.text, action.owner or "Belirlenmedi", format_tr_short(action.due_date),
               _tr_cap(action.status)]
        if extra is not None:
            row.append(extra)
        return row

    def _actions(self, ctx: MinutesContext, st: dict) -> list:
        actions = ctx.meeting.action_items
        if not actions:
            return []
        rows = [self._action_row(i, a) for i, a in enumerate(actions, start=1)]
        flowables = [self._table(["#", "Görev", "Sorumlu", "Son tarih", "Durum"], rows, st, [8, 78, 42, 22, 24])]
        missing = [a for a in actions if not a.owner or a.due_date is None]
        if missing:
            from reportlab.platypus import Paragraph  # noqa: PLC0415

            flowables.append(Paragraph(
                f"Not: {len(missing)} görevin sorumlusu veya son tarihi toplantıda belirlenmedi.", st["small"]))
        return flowables

    def _carryover(self, ctx: MinutesContext, st: dict) -> list:
        from reportlab.platypus import Paragraph  # noqa: PLC0415

        flowables: list = []
        if ctx.carryover_open:
            flowables.append(Paragraph("Açık devreden görevler", st["h3"]))
            rows = [self._action_row(i, c.action, f"{c.source_title} ({c.source_date})")
                    for i, c in enumerate(ctx.carryover_open, start=1)]
            flowables.append(self._table(["#", "Görev", "Sorumlu", "Son tarih", "Durum", "Verildiği toplantı"],
                                         rows, st, [8, 48, 30, 22, 24, 42]))
        if ctx.carryover_closed:
            flowables.append(Paragraph("Bu toplantıda kapatılan devreden görevler", st["h3"]))
            rows = [self._action_row(i, c.action, f"{c.source_title} ({c.source_date})")
                    for i, c in enumerate(ctx.carryover_closed, start=1)]
            flowables.append(self._table(["#", "Görev", "Sorumlu", "Son tarih", "Durum", "Verildiği toplantı"],
                                         rows, st, [8, 48, 30, 22, 24, 42]))
        return flowables

    def _votes(self, ctx: MinutesContext, st: dict) -> list:
        votes = ctx.meeting.votes
        if not votes:
            return []
        rows = [[str(i), v.topic, str(v.yes), str(v.no), str(v.abstain), v.result]
                for i, v in enumerate(votes, start=1)]
        return [self._table(["#", "Konu", "Lehte", "Aleyhte", "Çekimser", "Sonuç"], rows, st,
                            [8, 82, 18, 18, 20, 28])]

    def _parking(self, ctx: MinutesContext, st: dict) -> list:
        items = ctx.meeting.parking_lot
        if not items:
            return []
        rows = [[str(i), text] for i, text in enumerate(items, start=1)]
        return [self._table(["#", "Konu"], rows, st, [8, 166])]

    def _speakers(self, ctx: MinutesContext, st: dict) -> list:
        from reportlab.platypus import Paragraph, Spacer  # noqa: PLC0415

        tracker = SpeakerTracker(ctx.meeting)
        stats = tracker.stats(now=ctx.as_of)
        silent = tracker.silent_participants()
        if not stats and not silent:
            return []
        flowables: list = []
        if stats:
            total = sum(s.total_seconds for s in stats) or 1.0
            rows = [[s.name, s.minutes_text, str(s.turns), s.speech_text, f"%{round(100 * s.total_seconds / total)}"]
                    for s in stats]
            flowables.append(self._table(["Konuşmacı", "Söz süresi*", "Söz alma", "Kayda geçen konuşma**",
                                          "Söz payı*"], rows, st, [50, 34, 20, 46, 24]))
            flowables.append(Spacer(1, 3))
            flowables.extend(Paragraph(_p(note), st["note"]) for note in SPEAKER_NOTES)
        if silent:
            flowables.append(Paragraph(f"Söz almayan katılımcılar: {_p(', '.join(silent))}", st["note"]))
        return flowables

    def _transcript(self, ctx: MinutesContext, st: dict) -> list:
        from reportlab.platypus import Paragraph, Table, TableStyle  # noqa: PLC0415

        entries = ctx.meeting.transcript
        if not entries:
            return []
        data = [[Paragraph("Saat", st["head"]), Paragraph("Konuşmacı", st["head"]), Paragraph("Konuşma", st["head"])]]
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), st["primary"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, st["line"]),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]
        current_item: str | None = None
        for entry in entries:
            if entry.agenda_item != current_item:
                current_item = entry.agenda_item
                row = len(data)
                data.append([Paragraph(f"Gündem: {_p(current_item or 'Gündem dışı')}", st["cell_bold"]), "", ""])
                style.append(("SPAN", (0, row), (-1, row)))
                style.append(("BACKGROUND", (0, row), (-1, row), st["light"]))
            label = TranscriptKind.LABELS.get(entry.kind)
            text = f'<font color="{MUTED_COLOR}">[{_p(label)}] {_p(entry.text)}</font>' if label else _p(entry.text)
            data.append([Paragraph(f"{entry.timestamp:%H:%M:%S}", st["cell"]),
                         Paragraph(_p(entry.speaker), st["cell_bold"]),
                         Paragraph(text, st["cell"])])
        table = Table(data, colWidths=self._widths([17, 38, self._frame_width_mm - 55]), repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle(style))
        result: list = [table]
        if any(not e.is_speech for e in entries):
            result.append(Paragraph(_p(TRANSCRIPT_COMMAND_NOTE), st["small"]))
        return result

    # ---------------------------------------------------------------- tablo yardımcıları
    def _widths(self, widths_mm: list[float]) -> list[float]:
        """Oransal sütun genişliklerini yazı alanının tamamına ölçekler (pt)."""
        from reportlab.lib.units import mm  # noqa: PLC0415

        total = sum(widths_mm) or 1.0
        return [w * self._frame_width_mm / total * mm for w in widths_mm]

    @staticmethod
    def _table_style(st: dict) -> list:
        from reportlab.lib import colors  # noqa: PLC0415

        return [
            ("BACKGROUND", (0, 0), (-1, 0), st["primary"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, st["light"]]),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, st["line"]),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]

    def _table(self, headers: list[str], rows: list[list[str]], st: dict, widths_mm: list[float]):
        from reportlab.platypus import Paragraph, Table, TableStyle  # noqa: PLC0415

        data = [[Paragraph(_p(h), st["head"]) for h in headers]]
        data.extend([[Paragraph(_p(cell), st["cell"]) for cell in row] for row in rows])
        table = Table(data, colWidths=self._widths(widths_mm), repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle(self._table_style(st)))
        return table

    def _key_value_table(self, rows: list[tuple[str, str]], st: dict):
        from reportlab.platypus import Paragraph, Table, TableStyle  # noqa: PLC0415

        data = [[Paragraph(_p(tr_upper(k)), st["key"]), Paragraph(_p(v), st["value"])] for k, v in rows]
        table = Table(data, colWidths=self._widths([40, self._frame_width_mm - 40]), hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), st["light"]),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, st["line"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ]))
        return table

    def _page_decorator(self, ctx: MinutesContext, fonts: FontSet, logo=None):
        from reportlab.lib import colors  # noqa: PLC0415
        from reportlab.lib.units import mm  # noqa: PLC0415

        primary = _hex_color(self.primary_hex, DEFAULT_PRIMARY)
        accent = _hex_color(self.accent_hex, DEFAULT_ACCENT)
        muted = colors.HexColor(MUTED_COLOR)
        line = colors.HexColor(LINE_COLOR)
        company = self.company_name.strip()
        right_sub = f"J.A.R.V.I.S  |  {ctx.meeting.started_at:%d.%m.%Y}"
        generated = f"J.A.R.V.I.S ile {ctx.as_of:%d.%m.%Y %H:%M} tarihinde oluşturuldu"
        margin = MARGIN_MM * mm

        def decorate(canvas, doc) -> None:
            width, height = doc.pagesize
            canvas.saveState()
            canvas.setFillColor(primary)
            canvas.rect(0, height - 2.5 * mm, width, 2.5 * mm, fill=1, stroke=0)
            canvas.setFillColor(accent)
            canvas.rect(0, height - 3.1 * mm, width * 0.22, 0.6 * mm, fill=1, stroke=0)
            box_w, box_h = LOGO_BOX_MM[0] * mm, LOGO_BOX_MM[1] * mm
            top = height - 8 * mm
            if logo is not None:
                image_w, image_h = logo.getSize()
                scale = min(box_w / image_w, box_h / image_h)  # oran korunur, gerilmez
                draw_w, draw_h = image_w * scale, image_h * scale
                canvas.drawImage(logo, margin, top - draw_h, width=draw_w, height=draw_h, mask="auto")
            else:
                # Logo alanı ayrılır; şirket adı düz metin olarak yazılır (logo çizilmez/taklit edilmez).
                canvas.setFont(fonts.bold, 13)
                canvas.setFillColor(primary)
                canvas.drawString(margin, top - 6 * mm, (company or "J.A.R.V.I.S")[:40])
                canvas.setFont(fonts.regular, 6.2)
                canvas.setFillColor(muted)
                canvas.drawString(margin, top - 10.5 * mm, LOGO_PLACEHOLDER)
            canvas.setFont(fonts.bold, 8.2)
            canvas.setFillColor(primary)
            canvas.drawRightString(width - margin, top - 4 * mm, "TOPLANTI TUTANAĞI")
            canvas.setFont(fonts.regular, 7)
            canvas.setFillColor(muted)
            canvas.drawRightString(width - margin, top - 8.5 * mm, right_sub)
            canvas.setStrokeColor(line)
            canvas.setLineWidth(0.6)
            canvas.line(margin, height - 26 * mm, width - margin, height - 26 * mm)
            # alt bilgi: şirket unvanı + oluşturma bilgisi; "Sayfa X / Y" NumberedCanvas tarafından yazılır
            canvas.line(margin, 17 * mm, width - margin, 17 * mm)
            canvas.setFont(fonts.regular, 6.8)
            canvas.setFillColor(muted)
            canvas.drawString(margin, 12.5 * mm, tr_upper(company)[:90] if company else "J.A.R.V.I.S")
            canvas.drawString(margin, 9 * mm, generated)
            canvas.restoreState()

        return decorate


def _numbered_canvas_factory(font_name: str, color_hex: str = MUTED_COLOR):
    """"Sayfa X / Y" için tüm sayfalar bittikten sonra numara basan canvas."""
    from reportlab.lib import colors  # noqa: PLC0415
    from reportlab.lib.units import mm  # noqa: PLC0415
    from reportlab.pdfgen import canvas as rl_canvas  # noqa: PLC0415

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages: list[dict] = []

        def showPage(self):  # noqa: N802 (ReportLab API adı)
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self.setFont(font_name, 7)
                self.setFillColor(colors.HexColor(color_hex))
                width = self._pagesize[0]
                self.drawRightString(width - MARGIN_MM * mm, 12.5 * mm, f"Sayfa {self._pageNumber} / {total}")
                super().showPage()
            super().save()

    return NumberedCanvas


def _move_into_place(temp_path: Path, output_path: Path) -> Path:
    """Geçici dosyayı hedefe taşır. Hedef açıksa (Windows'ta PDF görüntüleyicide) yeni adla kaydeder."""
    try:
        os.replace(temp_path, output_path)
        return output_path
    except PermissionError:
        alternative = output_path.with_name(f"{output_path.stem}_{datetime.now():%H%M%S}{output_path.suffix}")
        os.replace(temp_path, alternative)
        log.warning("Hedef PDF kullanımda olduğu için yeni adla kaydedildi: %s", alternative)
        return alternative
