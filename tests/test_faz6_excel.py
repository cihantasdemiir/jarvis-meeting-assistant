"""Faz 6: görev listesini Excel'e aktarma. Dosya openpyxl ile yeniden açılarak doğrulanır."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from jarvis.meeting.manager import MeetingManager
from jarvis.reports.excel_export import CARRIED_DONE, CARRIED_OPEN, COLUMNS
from jarvis.voice.command_parser import CommandParser, IntentType

openpyxl = pytest.importorskip("openpyxl")
PARSER = CommandParser("jarvis", ["carvis"])


def load(path: str):
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["Görevler"]
    header = [c.value for c in sheet[1]]
    rows = [dict(zip(header, (c.value for c in row))) for row in sheet.iter_rows(min_row=2)]
    return workbook, sheet, rows


@pytest.fixture
def a(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


def db_fingerprint(settings) -> str:
    return hashlib.sha256(Path(settings.db_path).read_bytes()).hexdigest()


# ====================================================================== komutlar
@pytest.mark.parametrize("text,scope", [
    ("Jarvis, görevleri Excel'e aktar.", "meeting"), ("Jarvis, görev listesini excel olarak kaydet", "meeting"),
    ("Jarvis, açık görevleri Excel olarak çıkar.", "open"), ("Jarvis, tüm açık görevleri excel'e aktar", "open"),
])
def test_parser(text, scope):
    parsed = PARSER.parse(text)
    assert parsed.type == IntentType.EXPORT_TASKS and parsed.get("scope") == scope


def test_existing_commands_unchanged():
    assert PARSER.parse("Jarvis, açık görevleri oku").type == IntentType.OPEN_ACTIONS
    parsed = PARSER.parse("Jarvis, görev: Mehmet Excel tablosunu cumaya kadar güncellesin")
    assert parsed.type == IntentType.ADD_ACTION


# ====================================================================== içerik
def test_export_current_meeting(a):
    a.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    a.handle_text("Jarvis görev: Şirket içi eğitim planı güncellensin")
    r = a.handle_text("Jarvis, görevleri Excel'e aktar.")
    assert r.ok and "Görev listesi Excel dosyasına aktarıldı" in r.message
    workbook, sheet, rows = load(_last_xlsx(a))
    assert [c.value for c in sheet[1]] == [title for title, _ in COLUMNS]
    first = rows[0]
    assert first["Görev açıklaması"] == "raporu hazırlasın" and first["Sorumlu"] == "Mehmet Öztürk"
    assert isinstance(first["Son tarih"], datetime) and first["Son tarih"].date() == date(2026, 10, 16)
    assert sheet["E2"].number_format == "DD.MM.YYYY" and sheet["B2"].is_date
    second = rows[1]
    assert second["Görev açıklaması"] == "Şirket içi eğitim planı güncellensin"  # Türkçe karakter
    assert second["Sorumlu"] is None and second["Son tarih"] is None  # boş alanlara sahte veri yazılmaz
    assert first["Durum"] == "Açık" and first["Toplantı başlığı"] == "Test Toplantısı"
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:H3"
    assert sheet.column_dimensions["C"].width >= 40
    assert "Bilgi" in workbook.sheetnames


def _last_xlsx(assistant) -> str:
    files = sorted((assistant.minutes.settings.output_dir / "excel").glob("*.xlsx"))
    return str(files[-1])


def test_formula_injection_is_plain_text(a):
    a.handle_text('Jarvis görev: =HYPERLINK("http://kotu.example","tikla") yapılsın')
    a.handle_text("Jarvis görev: +90 hattı aranacak")
    a.handle_text("Jarvis görev: @SUM(A1) kontrol edilsin")
    a.handle_text("Jarvis, görevleri Excel'e aktar")
    _, sheet, rows = load(_last_xlsx(a))
    texts = [row["Görev açıklaması"] for row in rows]
    assert texts[0].startswith("=HYPERLINK") and sheet["C2"].data_type == "s"
    assert all(sheet.cell(row=i, column=3).data_type == "s" for i in range(2, 5))
    assert sheet["C2"].quotePrefix and sheet["C3"].quotePrefix


def test_open_completed_and_carried_tasks(repo, settings, clock, minutes):
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting("Ekim Haftalık 1")
    manager.add_action("Mehmet raporu cumaya kadar hazırlasın")
    manager.add_action("Ayşe sunumu haftaya kadar hazırlasın")
    manager.add_action("Ahmet bütçeyi kontrol etsin")
    first_ids = [x.id for x in manager.meeting.action_items]
    manager.end_meeting()
    clock.advance(days=7)
    manager.start_meeting("Ekim Haftalık 2")
    manager.complete_action("raporu hazırlasın")  # devreden görev bu toplantıda kapatıldı
    manager.add_action("Kenan tedarikçiyi arasın")
    manager.cancel_action("tedarikçiyi arasın")
    manager.add_action("Ayşe takvimi paylaşsın")
    result = minutes.export_tasks(manager.meeting)
    assert result.ok
    _, _, rows = load(result.data["path"])
    by_text = {row["Görev açıklaması"]: row for row in rows}
    assert by_text["raporu hazırlasın"]["Durum"] == CARRIED_DONE
    assert by_text["sunumu hazırlasın"]["Durum"] == CARRIED_OPEN
    assert by_text["sunumu hazırlasın"]["Toplantı başlığı"] == "Ekim Haftalık 1"
    assert by_text["sunumu hazırlasın"]["Kaynak toplantı"] != manager.meeting.id
    assert by_text["tedarikçiyi arasın"]["Durum"] == "İptal edildi"
    assert by_text["takvimi paylaşsın"]["Durum"] == "Açık"
    ids = [row["Görev kimliği"] for row in rows]
    assert len(ids) == len(set(ids))  # mükerrer yok
    assert set(first_ids) <= set(ids)


def test_open_scope_all_meetings_without_duplicates(repo, settings, clock, minutes):
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting("Toplantı A")
    manager.add_action("Mehmet raporu cumaya kadar hazırlasın")
    manager.add_action("Ayşe sunumu hazırlasın")
    manager.complete_action("sunumu hazırlasın")
    manager.end_meeting()
    clock.advance(days=7)
    manager.start_meeting("Toplantı B")
    manager.add_action("Ahmet bütçeyi kontrol etsin")
    result = minutes.export_tasks(manager.meeting, scope="open")
    _, _, rows = load(result.data["path"])
    texts = sorted(row["Görev açıklaması"] for row in rows)
    assert texts == ["bütçeyi kontrol etsin", "raporu hazırlasın"]  # tamamlanan yok, her görev bir kez
    assert "gorevler_acik_" in result.data["path"]


def test_past_meeting_export_ignores_later_meetings(repo, settings, clock, minutes):
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting("Eski")
    manager.add_action("Mehmet raporu hazırlasın")
    manager.end_meeting()
    old = manager.meeting
    clock.advance(days=7)
    manager.start_meeting("Yeni")
    manager.add_action("Ayşe sunumu hazırlasın")
    manager.end_meeting()
    _, _, rows = load(minutes.export_tasks(repo.get(old.id)).data["path"])
    assert [row["Görev açıklaması"] for row in rows] == ["raporu hazırlasın"]


def test_export_never_changes_database(a, settings):
    a.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    before_db = db_fingerprint(settings)
    before = json.dumps(a.manager.meeting.to_dict(), sort_keys=True, default=str)
    a.handle_text("Jarvis, görevleri Excel'e aktar")
    a.handle_text("Jarvis, açık görevleri Excel olarak çıkar")
    assert db_fingerprint(settings) == before_db
    assert json.dumps(a.manager.meeting.to_dict(), sort_keys=True, default=str) == before


def test_unique_file_names(a):
    a.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    first = a.handle_text("Jarvis, görevleri Excel'e aktar")
    second = a.handle_text("Jarvis, görevleri Excel'e aktar")  # aynı saniye (sabit test saati)
    files = sorted((a.minutes.settings.output_dir / "excel").glob("*.xlsx"))
    assert first.ok and second.ok and len(files) == 2
    assert all(f.name.startswith("gorevler_test-toplantisi_") for f in files)
    assert not list((a.minutes.settings.output_dir / "excel").glob("*.tmp*"))


def test_no_tasks_no_file(a):
    r = a.handle_text("Jarvis, görevleri Excel'e aktar")
    assert not r.ok and "Aktarılacak görev yok" in r.message
    assert not (a.minutes.settings.output_dir / "excel").exists()


def test_file_reopens_and_info_sheet(a):
    a.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    a.handle_text("Jarvis, görevleri Excel'e aktar")
    workbook, _, rows = load(_last_xlsx(a))
    info = {row[0].value: row[1].value for row in workbook["Bilgi"].iter_rows()}
    assert info["Toplam görev"] == 1 and info["Açık"] == 1 and isinstance(info["Oluşturma zamanı"], datetime)


def test_cli_flag():
    import main
    assert main.parse_args(["--export-tasks", "acik"]).export_tasks == "acik"
    assert main.parse_args(["--export-tasks"]).export_tasks == "son"
