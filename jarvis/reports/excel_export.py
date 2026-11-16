"""Görev listesini Excel'e (.xlsx) aktarma — Faz 6.

İki kapsam:
  * "meeting": bir toplantının görevleri (açık, tamamlanmış, iptal) + önceki toplantılardan devreden açık görevler
    + devredip bu toplantıda kapatılan görevler.
  * "open": tüm toplantılardaki açık görevler.

Kurallar:
  * Yalnızca OKUR: SQLite'a ve toplantı kaydına hiçbir şey yazılmaz.
  * Görev kimliğine göre tekilleştirilir; devreden görev iki kez yazılmaz.
  * Tarihler gerçek Excel tarih hücresidir (GG.AA.YYYY biçimi); boş sorumlu/son tarih boş bırakılır.
  * Formül enjeksiyonu: "=", "+", "-", "@" ile başlayan metinler formül değil düz metin olarak yazılır.
  * Dosya `output/excel/` altında anlamlı ve benzersiz adla, önce geçici dosyaya yazılıp sonra taşınır.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from ..meeting.models import ActionItem, ActionStatus, Meeting
from ..meeting.storage import MeetingRepository

COLUMNS: list[tuple[str, int]] = [
    ("Toplantı başlığı", 40), ("Toplantı tarihi", 15), ("Görev açıklaması", 52), ("Sorumlu", 22),
    ("Son tarih", 12), ("Durum", 46), ("Kaynak toplantı", 26), ("Görev kimliği", 18),
]
_STATUS = {ActionStatus.OPEN: "Açık", ActionStatus.DONE: "Tamamlandı", ActionStatus.CANCELLED: "İptal edildi"}
CARRIED_OPEN = "Açık — önceki toplantıdan devreden"
CARRIED_DONE = "Tamamlandı — devreden, bu toplantıda kapatıldı"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class ExcelUnavailable(RuntimeError):
    pass


@dataclass
class TaskRow:
    meeting_title: str
    meeting_date: date | None
    text: str
    owner: str
    due_date: date | None
    status: str
    source_meeting: str
    action_id: str


class TaskExporter:
    def __init__(self, repo: MeetingRepository, output_dir: Path, clock: Callable[[], datetime] = datetime.now):
        self.repo = repo
        self.output_dir = output_dir
        self.clock = clock

    # ---------------------------------------------------------------- satırlar
    def meeting_rows(self, meeting: Meeting) -> list[TaskRow]:
        rows: list[TaskRow] = []
        seen: set[str] = set()
        for action in meeting.action_items:
            rows.append(self._row(action, meeting.title, meeting.started_at.date(), meeting.id,
                                  _STATUS.get(action.status, action.status)))
            seen.add(action.id)
        carried_open = self.repo.get_open_actions(exclude_meeting_id=meeting.id, include_demo=meeting.is_demo)
        carried_done = self.repo.get_actions(meeting.carryover_completed)
        headers = self.repo.get_meeting_headers([a.meeting_id for a in carried_open + carried_done])
        for actions, status in ((carried_done, CARRIED_DONE), (carried_open, CARRIED_OPEN)):
            for action in actions:
                started = _date(headers.get(action.meeting_id, {}).get("started_at"))
                if action.id in seen or (status == CARRIED_OPEN and started and started > meeting.started_at.date()):
                    continue  # tekrar yok; sonraki toplantıların görevleri bu toplantıya "devreden" sayılmaz
                seen.add(action.id)
                header = headers.get(action.meeting_id, {})
                rows.append(self._row(action, header.get("title", ""), _date(header.get("started_at")),
                                      action.meeting_id, status))
        return rows

    def open_rows(self, current: Meeting | None = None, include_demo: bool = False) -> list[TaskRow]:
        """Tüm açık görevler. Açık toplantının bellekteki hali önceliklidir (kimliğe göre tekil)."""
        rows: list[TaskRow] = []
        seen: set[str] = set()
        if current is not None:
            for action in current.action_items:
                if action.status == ActionStatus.OPEN:
                    rows.append(self._row(action, current.title, current.started_at.date(), current.id, "Açık"))
                    seen.add(action.id)
        others = self.repo.get_open_actions(exclude_meeting_id=current.id if current else None,
                                            include_demo=include_demo or bool(current and current.is_demo))
        headers = self.repo.get_meeting_headers([a.meeting_id for a in others])
        for action in others:
            if action.id in seen:
                continue
            seen.add(action.id)
            header = headers.get(action.meeting_id, {})
            status = CARRIED_OPEN if current is not None else "Açık"
            rows.append(self._row(action, header.get("title", ""), _date(header.get("started_at")),
                                  action.meeting_id, status))
        return rows

    @staticmethod
    def _row(action: ActionItem, title: str, day: date | None, meeting_id: str, status: str) -> TaskRow:
        return TaskRow(title, day, action.text, action.owner or "", action.due_date, status, meeting_id, action.id)

    # ---------------------------------------------------------------- dosya
    def export(self, rows: list[TaskRow], scope_label: str, name_hint: str) -> Path:
        try:
            from openpyxl import Workbook  # noqa: PLC0415
            from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
            from openpyxl.utils import get_column_letter  # noqa: PLC0415
        except ImportError as exc:
            raise ExcelUnavailable("Excel dışa aktarımı için openpyxl gerekli: "
                                   "pip install -r requirements.txt") from exc
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Görevler"
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="1F3A5F")
        for column, (title, width) in enumerate(COLUMNS, start=1):
            cell = sheet.cell(row=1, column=column, value=title)
            cell.font, cell.fill = header_font, header_fill
            cell.alignment = Alignment(vertical="center")
            sheet.column_dimensions[get_column_letter(column)].width = width
        for index, row in enumerate(rows, start=2):
            values = [row.meeting_title, row.meeting_date, row.text, row.owner or None, row.due_date, row.status,
                      row.source_meeting, row.action_id]
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=index, column=column)
                if isinstance(value, date):
                    cell.value = value
                    cell.number_format = "DD.MM.YYYY"
                elif value is None or value == "":
                    cell.value = None  # boş alan bilinçli olarak boş: sahte veri yazılmaz
                else:
                    _set_text(cell, str(value))
            sheet.cell(row=index, column=3).alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
        sheet.page_setup.orientation = "landscape"  # yazdırırken tüm sütunlar tek sayfa genişliğinde
        sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.print_title_rows = "1:1"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(1, len(rows) + 1)}"
        info = workbook.create_sheet("Bilgi")
        counts = _counts(rows)
        for line in [("Oluşturan", "J.A.R.V.I.S Toplantı Asistanı"), ("Oluşturma zamanı", self.clock()),
                     ("Kapsam", scope_label), ("Toplam görev", len(rows)), *counts,
                     ("Not", "Boş sorumlu ve son tarih alanları kayıtta belirtilmediği için boştur."),
                     ("Not", "Kaynak toplantı: görevin kaydedildiği toplantının kimliği.")]:
            info.append([line[0], line[1]])
            if isinstance(line[1], str):
                _set_text(info.cell(row=info.max_row, column=2), line[1])
            elif isinstance(line[1], datetime):
                info.cell(row=info.max_row, column=2).number_format = "DD.MM.YYYY HH:MM"
        info.column_dimensions["A"].width = 22
        info.column_dimensions["B"].width = 70
        path = self._unique_path(name_hint)
        temp = path.with_suffix(".tmp.xlsx")
        workbook.save(temp)
        os.replace(temp, path)
        return path

    def _unique_path(self, name_hint: str) -> Path:
        folder = self.output_dir / "excel"
        folder.mkdir(parents=True, exist_ok=True)
        base = f"gorevler_{name_hint}_{self.clock():%Y%m%d-%H%M%S}"
        path = folder / f"{base}.xlsx"
        counter = 2
        while path.exists():
            path = folder / f"{base}_{counter}.xlsx"
            counter += 1
        return path


def _set_text(cell, text: str) -> None:
    """Formül enjeksiyonuna karşı: metin her zaman düz metin hücresi olarak yazılır."""
    cell.value = text
    cell.data_type = "s"
    if text.startswith(_FORMULA_START):
        cell.quotePrefix = True


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _counts(rows: list[TaskRow]) -> list[tuple[str, int]]:
    def count(predicate) -> int:
        return sum(1 for row in rows if predicate(row.status))
    return [("Açık", count(lambda s: s.startswith("Açık"))), ("Tamamlandı", count(lambda s: s.startswith("Tamam"))),
            ("İptal edildi", count(lambda s: s.startswith("İptal"))),
            ("Devreden", count(lambda s: "devreden" in s))]
