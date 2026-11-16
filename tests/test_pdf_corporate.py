"""Kurumsal PDF görünümü: üst/alt bilgi, logo alanı, boş bölüm gizleme, uzun tablolar,
Türkçe karakter, LLM taslaklarının resmî tutanağa girmemesi. Gerçek logo, SMTP veya ağ kullanılmaz.
"""
from __future__ import annotations

import email
import re
from datetime import date, datetime, timedelta
from email import policy
from pathlib import Path

import pytest

from jarvis.meeting.models import (
    ActionItem,
    AgendaItem,
    AiDraft,
    Decision,
    DraftKind,
    DraftStatus,
    Meeting,
    MeetingStatus,
    Participant,
    TranscriptEntry,
)
from jarvis.reports.pdf_generator import LOGO_PLACEHOLDER, MinutesContext, MinutesPdfGenerator

pypdf = pytest.importorskip("pypdf")

T0 = datetime(2026, 10, 14, 10, 0, 0)
COMPANY = "Örnek Demir Çelik San. ve Tic. A.Ş."
TURKISH = "çÇğĞıİöÖşŞüÜ"


def read(path: Path) -> tuple[list[str], str]:
    pages = [page.extract_text() or "" for page in pypdf.PdfReader(str(path)).pages]
    return pages, "\n".join(pages)


def flat(text: str) -> str:
    return " ".join(text.split())


def meeting(**overrides) -> Meeting:
    base = Meeting(
        title="Haftalık Üretim ve Değerlendirme Toplantısı", started_at=T0, ended_at=T0 + timedelta(minutes=45),
        chair="Kenan Çetin", location="Çayırova Toplantı Salonu", status=MeetingStatus.ENDED,
        participants=[Participant("Ayşe Yılmaz", "ayse.gizli@example.com", "Üretim Planlama", True),
                      Participant("Mehmet Öztürk", "mehmet.gizli@example.com", "Kalite", True),
                      Participant("Ahmet Yılmaz", "", "Bakım", False)],
        agenda=[AgendaItem("Üretim planlaması", 15, "Ayşe Yılmaz", "tamamlandı", elapsed_seconds=840)],
        decisions=[Decision("Yeni üretim planı pazartesi uygulanacak", "Üretim planlaması", T0)],
        action_items=[ActionItem("Kalite raporunu hazırla", "Mehmet Öztürk", date(2026, 10, 16), meeting_id="m")],
        transcript=[TranscriptEntry(T0 + timedelta(minutes=3), "Ayşe Yılmaz", f"Kapasite tablosu {TURKISH}",
                                    "Üretim planlaması")],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def build(tmp_path: Path, m: Meeting, name: str = "t.pdf", **kwargs) -> tuple[MinutesPdfGenerator, Path]:
    generator = MinutesPdfGenerator(company_name=COMPANY, primary_color="#143448", **kwargs)
    path = generator.generate(MinutesContext(m, as_of=T0 + timedelta(hours=1)), tmp_path / name)
    return generator, path


def png(path: Path, size: tuple[int, int] = (1200, 360), fmt: str = "PNG") -> Path:
    from PIL import Image  # reportlab bağımlılığı

    Image.new("RGBA", size, (120, 120, 120, 255)).save(path, fmt)  # nötr test görseli, logo değildir
    return path


# ====================================================================== üst / alt bilgi
def test_every_page_has_header_footer_and_page_number(tmp_path):
    many = [TranscriptEntry(T0 + timedelta(seconds=i), "Ayşe Yılmaz", f"Satır {i} {TURKISH}", "Üretim planlaması")
            for i in range(150)]
    _, path = build(tmp_path, meeting(transcript=many))
    pages, _ = read(path)
    assert len(pages) >= 4
    for number, page in enumerate(pages, start=1):
        assert "TOPLANTI TUTANAĞI" in page
        assert "ÖRNEK DEMİR ÇELİK SAN. VE TİC. A.Ş." in page  # Türkçe büyük harf (İ) doğru
        assert f"Sayfa {number} / {len(pages)}" in page
        assert "J.A.R.V.I.S ile 14.10.2026 11:00 tarihinde oluşturuldu" in page


def test_turkish_characters_everywhere(tmp_path):
    _, path = build(tmp_path, meeting())
    _, text = read(path)
    for word in [TURKISH, "Haftalık Üretim ve Değerlendirme Toplantısı", "Çayırova Toplantı Salonu",
                 "Mehmet Öztürk", "Yönetici Özeti", "Konuşma"]:
        assert word in text, word


# ====================================================================== logo
def test_without_logo_area_is_marked_on_every_page(tmp_path):
    generator, path = build(tmp_path, meeting())
    pages, _ = read(path)
    assert all(LOGO_PLACEHOLDER in flat(page) for page in pages)
    assert generator.warnings == []  # logo tanımlanmamış: hata değil


def test_missing_logo_file_warns_and_keeps_area(tmp_path):
    generator, path = build(tmp_path, meeting(), logo_path=tmp_path / "yok.png")
    assert path.is_file() and any("Logo dosyası bulunamadı" in w for w in generator.warnings)
    assert LOGO_PLACEHOLDER in flat(read(path)[1])


def test_corrupt_logo_does_not_break_pdf(tmp_path):
    bad = tmp_path / "bozuk.png"
    bad.write_bytes(b"logo degil")
    generator, path = build(tmp_path, meeting(), logo_path=bad)
    assert path.is_file() and any("Logo okunamadı" in w for w in generator.warnings)


@pytest.mark.parametrize("fmt,suffix", [("PNG", ".png"), ("WEBP", ".webp"), ("JPEG", ".jpg")])
def test_logo_file_is_embedded_on_every_page(tmp_path, fmt, suffix):
    image = png(tmp_path / f"logo{suffix}", fmt=fmt) if fmt != "JPEG" else _jpeg(tmp_path / "logo.jpg")
    many = [TranscriptEntry(T0, "Ayşe Yılmaz", f"Satır {i}", "Üretim planlaması") for i in range(120)]
    generator, path = build(tmp_path, meeting(transcript=many), logo_path=image)
    assert generator.warnings == []
    reader = pypdf.PdfReader(str(path))
    for page in reader.pages:
        xobjects = page["/Resources"].get("/XObject") or {}
        assert any(obj.get_object().get("/Subtype") == "/Image" for obj in xobjects.values())
        assert LOGO_PLACEHOLDER not in flat(page.extract_text())


def _jpeg(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (900, 300), (120, 120, 120)).save(path, "JPEG")
    return path


# ====================================================================== bölümler
def test_empty_sections_are_not_rendered(tmp_path):
    m = meeting(decisions=[], action_items=[], votes=[], parking_lot=[], speaker_turns=[], agenda=[],
                next_meeting_date=None)
    _, path = build(tmp_path, m)
    text = flat(read(path)[1])
    for absent in ["Alınan Kararlar", "Görevler", "Devreden İşler", "Oylamalar", "Otopark", "| Gündem",
                   "Söz süresi", "Sonraki Toplantı"]:
        assert absent not in text, absent
    numbers = [int(n) for n in re.findall(r"(\d{2}) \| [A-ZÇĞİÖŞÜ]", text)]
    assert numbers == list(range(1, len(numbers) + 1))


def test_no_participants_no_attendance_table(tmp_path):
    _, path = build(tmp_path, meeting(participants=[]))
    text = flat(read(path)[1])
    assert "| Katılım" not in text and "Katılımcı listesi yok" in text


def test_participant_emails_are_not_printed(tmp_path):
    _, path = build(tmp_path, meeting())
    text = read(path)[1]
    assert "gizli@example.com" not in text and "Üretim Planlama" in text


def test_next_meeting_time_never_invented(tmp_path):
    _, path = build(tmp_path, meeting(next_meeting_date=date(2026, 10, 21)))
    text = flat(read(path)[1])
    assert "Sonraki Toplantı 21 Ekim 2026 Çarşamba" in text and "saat" not in text.split("Sonraki Toplantı")[1][:40]
    _, path = build(tmp_path, meeting(next_meeting_date=date(2026, 10, 21), next_meeting_time="14:00"), "s.pdf")
    assert "21 Ekim 2026 Çarşamba, saat 14.00" in flat(read(path)[1])


def test_llm_drafts_are_not_in_official_minutes(tmp_path):
    drafts = [AiDraft(1, DraftKind.DECISION, "Onaysız taslak karar metni", status=DraftStatus.DRAFT, source="llm:q"),
              AiDraft(2, DraftKind.ACTION, "Reddedilmiş taslak görev metni", owner="Ayşe Yılmaz",
                      status=DraftStatus.REJECTED, source="llm:q"),
              AiDraft(3, DraftKind.SUGGESTION, "Onaysız öneri metni", source="llm:q")]
    _, path = build(tmp_path, meeting(ai_drafts=drafts))
    text = flat(read(path)[1])
    assert "Onaysız taslak karar" not in text and "Reddedilmiş taslak" not in text and "öneri metni" not in text
    assert "Yeni üretim planı pazartesi uygulanacak" in text  # kayıtlı karar basılır


# ====================================================================== uzun içerik
def test_twenty_plus_tasks_split_with_repeated_header(tmp_path):
    actions = [ActionItem(f"Görev {i:02d}: Çelik bobin sevkiyat planını gözden geçir " + "ayrıntılı " * 8,
                          "Mehmet Öztürk", date(2026, 10, 20), meeting_id="m") for i in range(45)]
    _, path = build(tmp_path, meeting(action_items=actions))
    pages, text = read(path)
    task_pages = [p for p in pages if "Görev 0" in p or "Görev 1" in p or "Görev 4" in p]
    assert len(task_pages) >= 2
    assert all("Sorumlu" in p and "Son tarih" in p for p in task_pages)  # başlık satırı her sayfada tekrar
    assert "Görev 00:" in text and "Görev 44:" in text


def test_long_unbroken_turkish_word_wraps(tmp_path):
    word = "Şehirlerarasıçelikbobinsevkiyatplanlamasıdeğerlendirmesiçalışmasıüzerineöneriler"
    m = meeting(decisions=[Decision(word, "Üretim planlaması", T0)],
                transcript=[TranscriptEntry(T0, "Ayşe Yılmaz", word * 2, "Üretim planlaması")])
    _, path = build(tmp_path, m)
    assert word[:20] in flat(read(path)[1]).replace(" ", "")


def test_hundred_plus_transcript_lines_complete(tmp_path):
    many = [TranscriptEntry(T0 + timedelta(seconds=5 * i), "Mehmet Öztürk",
                            f"Konuşma {i}: ölçüm sonuçları değerlendirildi " + "kelime " * 30, "Üretim planlaması")
            for i in range(130)]
    _, path = build(tmp_path, meeting(transcript=many))
    pages, text = read(path)
    assert "Konuşma 0:" in text and "Konuşma 129:" in text
    transcript_pages = [p for p in pages if "Konuşma 1" in p]
    assert all("Konuşmacı" in p for p in transcript_pages)


# ====================================================================== uçtan uca (dry-run)
def test_same_pdf_in_dry_run_email(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis Kenan, Ayşe ve Mehmet katıldı")
    assistant.handle_text("Jarvis karar: Çelik tedarik planı onaylandı")
    assistant.handle_text("Jarvis toplantıyı bitir")
    stored = assistant.manager.repo.get(assistant.manager.meeting.id)
    pdf = Path(stored.minutes_pdf)
    result = assistant.minutes.send_minutes(stored)
    assert result.ok and result.data["mode"] == "dry-run"
    saved = email.message_from_bytes(Path(result.data["eml"]).read_bytes(), policy=policy.default)
    attached = next(p for p in saved.iter_attachments() if (p.get_filename() or "").endswith(".pdf"))
    assert attached.get_payload(decode=True) == pdf.read_bytes()
    text = flat(read(pdf)[1])
    assert "Çelik tedarik planı onaylandı" in text and "Kenan Çetin" in text and "TOPLANTI TUTANAĞI" in text


# ====================================================================== son rötuşlar: Ek A ve dipnotlar
def _positions(path: Path) -> list[tuple[int, str, float]]:
    """(sayfa, metin parçası, y konumu pt) — taşma kontrolü için."""
    found: list[tuple[int, str, float]] = []
    for number, page in enumerate(pypdf.PdfReader(str(path)).pages, start=1):
        def visit(text, cm, tm, font, size, number=number):
            if text.strip():
                found.append((number, text, tm[5] * cm[3] + cm[5]))
        page.extract_text(visitor_text=visit)
    return found


def test_no_transcript_no_appendix_and_no_empty_page(tmp_path):
    with_text = build(tmp_path, meeting(), "a.pdf")[1]
    without = build(tmp_path, meeting(transcript=[]), "b.pdf")[1]
    pages_with, text_with = read(with_text)
    pages_without, text_without = read(without)
    assert "Ek A" in text_with and "Ek A" not in text_without
    assert "transkript kaydı yok" not in text_without
    assert len(pages_without) == len(pages_with) - 1
    assert all(len(flat(p)) > 200 for p in pages_without)  # boş (yalnızca üst/alt bilgili) sayfa yok
    for number, page in enumerate(pages_without, start=1):
        assert f"Sayfa {number} / {len(pages_without)}" in page


def test_llm_drafts_do_not_create_appendix(tmp_path):
    drafts = [AiDraft(1, DraftKind.DECISION, "Onaysız taslak", status=DraftStatus.DRAFT, source="llm:q")]
    _, path = build(tmp_path, meeting(transcript=[], ai_drafts=drafts))
    assert "Ek A" not in read(path)[1]


def test_single_transcript_entry_creates_appendix_after_minutes(tmp_path):
    _, path = build(tmp_path, meeting())
    pages, _ = read(path)
    assert "Ek A — Toplantı Transkripti" in pages[-1] and "Ek A" not in "".join(pages[:-1])
    assert f"Kapasite tablosu {TURKISH}" in pages[-1]


def test_long_transcript_page_numbers_consistent(tmp_path):
    many = [TranscriptEntry(T0 + timedelta(seconds=i), "Ayşe Yılmaz", f"Bölüm {i}: {TURKISH} " + "söz " * 40,
                            "Üretim planlaması") for i in range(200)]
    _, path = build(tmp_path, meeting(transcript=many))
    pages, text = read(path)
    assert len(pages) >= 6 and "Bölüm 0:" in text and "Bölüm 199:" in text
    first_appendix = next(i for i, p in enumerate(pages) if "Ek A" in p)
    assert all("Bölüm" not in p for p in pages[:first_appendix])  # transkript tutanaktan sonra gelir
    for number, page in enumerate(pages, start=1):
        assert f"Sayfa {number} / {len(pages)}" in page


def test_no_speaker_stats_no_footnotes(tmp_path):
    _, path = build(tmp_path, meeting(speaker_turns=[]))
    text = flat(read(path)[1])
    assert "Söz süresi:" not in text and "Kayda geçen konuşma:" not in text


def test_speaker_footnotes_readable_two_paragraphs_no_overflow(tmp_path):
    from reportlab.lib.units import mm

    from jarvis.meeting.models import SpeakerTurn
    from jarvis.reports.pdf_generator import BOTTOM_MARGIN_MM, SPEAKER_NOTES

    people = [Participant(f"Katılımcı Şükrü {i:02d}", "", "Üretim", True) for i in range(40)]
    turns = [SpeakerTurn(p.name, T0 + timedelta(minutes=i), T0 + timedelta(minutes=i, seconds=50), "Üretim planlaması")
             for i, p in enumerate(people)]
    _, path = build(tmp_path, meeting(participants=people, speaker_turns=turns))
    text = flat(read(path)[1])
    assert flat(SPEAKER_NOTES[0]) in text and flat(SPEAKER_NOTES[1]) in text
    assert "gerçekten konuştuğu süre değildir" in text  # anlam korunur
    notes = [(page, y) for page, part, y in _positions(path) if "Söz süresi:" in part or "Kayda geçen" in part]
    assert len(notes) >= 2
    assert all(y > BOTTOM_MARGIN_MM * mm for _, y in notes)  # alt bilgiye taşmaz


def test_footnote_style_is_larger_than_before():
    from jarvis.reports.fonts import register_turkish_fonts

    styles = MinutesPdfGenerator()._styles(register_turkish_fonts())
    assert styles["note"].fontSize >= 8 and styles["note"].leading >= 1.4 * styles["note"].fontSize
