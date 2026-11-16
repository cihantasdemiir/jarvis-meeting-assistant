"""PDF tutanak testleri (G-10, T-06): Türkçe karakter, sayfa numarası, çok sayfalı tablolar."""
import re
import struct
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.meeting.models import (
    ActionItem,
    AgendaItem,
    Decision,
    Meeting,
    MeetingStatus,
    Participant,
    SpeakerTurn,
    TranscriptEntry,
    Vote,
)
from jarvis.reports.fonts import FontError, register_turkish_fonts
from jarvis.reports.pdf_generator import CarriedAction, MinutesContext, MinutesPdfGenerator

pypdf = pytest.importorskip("pypdf")

T0 = datetime(2026, 10, 14, 10, 0, 0)
TURKISH = "çÇğĞıİöÖşŞüÜ"


def pdf_text(path: Path) -> tuple[list[str], str]:
    reader = pypdf.PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return pages, "\n".join(pages)


def make_meeting(transcript_lines: int = 3) -> Meeting:
    meeting = Meeting(
        title="Şubat Ayı Üretim İstişaresi", started_at=T0, ended_at=T0 + timedelta(minutes=50),
        chair="Kenan Çetin", location="İzmir Ofisi", status=MeetingStatus.ENDED,
        participants=[Participant("Ayşe Yılmaz", "ayse@example.com", "Planlama", True),
                      Participant("Mehmet Öztürk", "mehmet@example.com", "Kalite", True),
                      Participant("Zeynep Şahin", "", "İK", False)],
        agenda=[AgendaItem("Üretim planlaması", 15, "Ayşe Yılmaz", "tamamlandı", elapsed_seconds=900,
                           notes=["Hat 2 bakımda"]),
                AgendaItem("Çeşitli", 5, status="ertelendi")],
        decisions=[Decision(f"Karar {TURKISH} <özel> & işaretler", "Üretim planlaması", T0)],
        action_items=[ActionItem("Raporu hazırla", "Mehmet Öztürk", date(2026, 10, 21), meeting_id="x"),
                      ActionItem("Tedarikçiyle görüş", "", None, meeting_id="x")],
        votes=[Vote("Fazla mesai", 2, 0, 1)],
        parking_lot=["Ofis taşınması"],
        speaker_turns=[SpeakerTurn("Ayşe Yılmaz", T0, T0 + timedelta(minutes=4), "Üretim planlaması")],
        next_meeting_date=date(2026, 10, 21),
        summary="Kapanış özeti metni.",
    )
    meeting.transcript = [
        TranscriptEntry(T0 + timedelta(seconds=10 * i), "Ayşe Yılmaz",
                        f"Satır {i}: Türkçe konuşma {TURKISH} uzun metin " + "kelime " * 25,
                        "Üretim planlaması" if i < transcript_lines // 2 else "Çeşitli")
        for i in range(transcript_lines)
    ]
    return meeting


@pytest.fixture
def generator() -> MinutesPdfGenerator:
    return MinutesPdfGenerator(company_name="Örnek Sanayi A.Ş.")


def test_font_supports_turkish():
    fonts = register_turkish_fonts()
    assert fonts.regular_path.is_file()


def test_font_error_when_nothing_found(monkeypatch, tmp_path):
    import jarvis.reports.fonts as fonts_module

    monkeypatch.setattr(fonts_module, "_CANDIDATES", [])
    monkeypatch.setattr(fonts_module, "_registered", None)
    with pytest.raises(FontError, match="assets/fonts"):
        fonts_module.register_turkish_fonts(tmp_path / "yok.ttf")


def test_t06_turkish_characters_and_page_numbers(generator, tmp_path):
    path = generator.generate(MinutesContext(make_meeting(), as_of=T0 + timedelta(hours=1)), tmp_path / "t.pdf")
    pages, text = pdf_text(path)
    assert TURKISH in text
    for word in ["Şubat Ayı Üretim İstişaresi", "İzmir Ofisi", "Örnek Sanayi A.Ş.", "Alınan Kararlar",
                 "Konuşmacı İstatistikleri", "Ek A — Toplantı Transkripti", "Mehmet Öztürk"]:
        assert word in text, word
    assert "<özel> & işaretler" in text  # XML özel karakterleri PDF'i bozmaz
    for number, page in enumerate(pages, start=1):
        assert f"Sayfa {number} / {len(pages)}" in page


def test_sections_numbered_and_empty_ones_skipped(generator, tmp_path):
    meeting = make_meeting()
    meeting.votes = []
    meeting.parking_lot = []
    path = generator.generate(MinutesContext(meeting, as_of=T0), tmp_path / "s.pdf")
    _, text = pdf_text(path)
    assert "Oylamalar" not in text and "Otopark" not in text
    numbers = [int(n) for n in re.findall(r"^(\d{2}) \| [A-ZÇĞİÖŞÜ]", text, flags=re.MULTILINE)]
    assert len(numbers) >= 5 and numbers == list(range(1, len(numbers) + 1))


def test_long_transcript_spans_many_pages(generator, tmp_path):
    path = generator.generate(MinutesContext(make_meeting(transcript_lines=400), as_of=T0), tmp_path / "l.pdf")
    pages, text = pdf_text(path)
    assert len(pages) >= 6
    assert "Satır 0:" in text and "Satır 399:" in text
    # Tablo başlığı her transkript sayfasında tekrarlanır
    transcript_pages = [p for p in pages if "Satır " in p]
    assert all("Konuşmacı" in p for p in transcript_pages)


def test_empty_meeting_still_produces_pdf(generator, tmp_path):
    meeting = Meeting(title="Boş", started_at=T0, ended_at=T0, status=MeetingStatus.ENDED)
    path = generator.generate(MinutesContext(meeting, as_of=T0), tmp_path / "e.pdf")
    pages, text = pdf_text(path)
    assert "Ek A" not in text and len(pages) == 1  # transkript yoksa ek ve boş sayfa oluşmaz


def test_interim_and_demo_badges(generator, tmp_path):
    meeting = make_meeting()
    meeting.status = MeetingStatus.ACTIVE
    meeting.ended_at = None
    meeting.is_demo = True
    path = generator.generate(MinutesContext(meeting, as_of=T0 + timedelta(minutes=20), interim=True),
                              tmp_path / "i.pdf")
    _, text = pdf_text(path)
    assert "ARA TUTANAK" in text and "DEMO VERİSİ" in text and "devam ediyor" in text


def test_carryover_sections(generator, tmp_path):
    ctx = MinutesContext(
        make_meeting(), as_of=T0,
        carryover_open=[CarriedAction(ActionItem("Eski açık iş", "Ayşe Yılmaz"), "Önceki", "07.10.2026")],
        carryover_closed=[CarriedAction(ActionItem("Eski kapanan iş", "Mehmet Öztürk", status="tamamlandı"),
                                        "Önceki", "07.10.2026")],
    )
    _, text = pdf_text(generator.generate(ctx, tmp_path / "c.pdf"))
    assert "Açık devreden görevler" in text and "Eski açık iş" in text
    assert "Bu toplantıda kapatılan devreden görevler" in text and "Eski kapanan iş" in text


def _tiny_png(path: Path) -> Path:
    raw = b"\x00\xff\x00\x00" * 4  # 2x2 kırmızı
    rows = b"".join(b"\x00" + raw[i * 6:(i + 1) * 6] for i in range(2))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    return path


def test_logo_configurable(tmp_path):
    logo = _tiny_png(tmp_path / "logo.png")
    generator = MinutesPdfGenerator(logo_path=logo)
    path = generator.generate(MinutesContext(make_meeting(), as_of=T0), tmp_path / "logo.pdf")
    assert path.is_file() and generator.warnings == []


def test_missing_logo_is_warning_not_failure(tmp_path):
    generator = MinutesPdfGenerator(logo_path=tmp_path / "yok.png")
    path = generator.generate(MinutesContext(make_meeting(), as_of=T0), tmp_path / "nologo.pdf")
    assert path.is_file()
    assert any("Logo" in w for w in generator.warnings)


def test_no_temp_files_left_behind(generator, tmp_path):
    out = tmp_path / "out"
    generator.generate(MinutesContext(make_meeting(), as_of=T0), out / "a.pdf")
    assert [p.name for p in out.iterdir()] == ["a.pdf"]


def test_pdf_metadata(generator, tmp_path):
    path = generator.generate(MinutesContext(make_meeting(), as_of=T0), tmp_path / "m.pdf")
    info = pypdf.PdfReader(str(path)).metadata
    assert "Şubat Ayı Üretim İstişaresi" in info.title
