"""Katılımcı ve gündem dosyalarını yükler.

Dosya eksik veya bozuksa program çökmez; boş liste ve açıklayıcı bir uyarı
döndürülür (PDF T-02, talimat Bölüm 11 madde 9-10).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import AgendaItem, Participant

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class LoadResult:
    items: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_json(path: Path, label: str) -> tuple[Any, list[str]]:
    if not path.exists():
        return None, [f"{label} dosyası bulunamadı: {path.name}"]
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle), []
    except json.JSONDecodeError as exc:
        return None, [f"{label} dosyası bozuk (satır {exc.lineno}, sütun {exc.colno}): {path.name}"]
    except OSError as exc:
        return None, [f"{label} dosyası okunamadı: {path.name} ({exc})"]


def _as_list(data: Any, key: str) -> list[Any] | None:
    """Hem düz liste hem de {"participants": [...]} biçimini kabul eder."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data[key]
    return None


def load_participants(path: Path) -> LoadResult:
    result = LoadResult()
    data, warnings = _read_json(path, "Katılımcı")
    result.warnings.extend(warnings)
    if data is None:
        return result
    records = _as_list(data, "participants")
    if records is None:
        result.warnings.append("Katılımcı dosyası liste biçiminde değil.")
        return result
    seen: set[str] = set()
    for index, record in enumerate(records, start=1):
        if isinstance(record, str):
            record = {"name": record}
        if not isinstance(record, dict) or not str(record.get("name", "")).strip():
            result.warnings.append(f"Katılımcı kaydı {index} atlandı: isim yok.")
            continue
        participant = Participant.from_dict({**record, "present": False})
        key = participant.name.casefold()
        if key in seen:
            result.warnings.append(f"Aynı katılımcı iki kez tanımlı, biri atlandı: {participant.name}")
            continue
        seen.add(key)
        if participant.email and not _EMAIL_RE.match(participant.email):
            result.warnings.append(
                f"{participant.name} için e-posta adresi geçersiz, tutanak gönderilmeyecek: {participant.email}")
            participant.email = ""
        result.items.append(participant)
    return result


def load_agenda(path: Path, default_minutes: int) -> LoadResult:
    result = LoadResult()
    data, warnings = _read_json(path, "Gündem")
    result.warnings.extend(warnings)
    if data is None:
        return result
    records = _as_list(data, "agenda")
    if records is None:
        result.warnings.append("Gündem dosyası liste biçiminde değil.")
        return result
    for index, record in enumerate(records, start=1):
        if isinstance(record, str):
            record = {"title": record}
        if not isinstance(record, dict) or not str(record.get("title", "")).strip():
            result.warnings.append(f"Gündem maddesi {index} atlandı: başlık yok.")
            continue
        minutes_raw = record.get("planned_minutes") or record.get("minutes") or default_minutes
        try:
            minutes = int(minutes_raw)
            if minutes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            result.warnings.append(f"'{record['title']}' maddesinin süresi geçersiz; {default_minutes} dk kullanıldı.")
            minutes = default_minutes
        result.items.append(AgendaItem(
            title=str(record["title"]).strip(),
            planned_minutes=minutes,
            presenter=str(record.get("presenter") or "").strip(),
        ))
    return result
