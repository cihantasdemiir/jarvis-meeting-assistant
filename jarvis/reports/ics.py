"""Sonraki toplantı için takvim daveti (.ics, RFC 5545) — Faz 6.

  * Saat dilimi: Europe/Istanbul. 2016'dan beri yaz saati uygulanmadığı için VTIMEZONE sabit +03:00 tanımlanır;
    takvim uygulamaları saati doğru gösterir.
  * METHOD:PUBLISH: e-postayla gelen dosya takvime eklenecek bir etkinlik olarak açılır. Katılım yanıtı (RSVP)
    toplamaz; J.A.R.V.I.S düzenleyici gibi davranmaz.
  * UID toplantıya göre sabittir. Tarih/saat değişirse aynı dosya SEQUENCE artırılarak güncellenir; takvimde
    ikinci (kopya) etkinlik oluşmaz. İçerik aynıysa dosya yeniden yazılmaz.
  * Metin alanlarında RFC 5545 kaçışları (\\, ;, ,, satır sonu) ve 75 baytlık satır katlama UTF-8 güvenli yapılır.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZID = "Europe/Istanbul"
PRODID = "-//J.A.R.V.I.S Toplanti Asistani//TR"
_VTIMEZONE = [
    "BEGIN:VTIMEZONE", f"TZID:{TZID}", "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0300",
    "TZOFFSETTO:+0300", "TZNAME:+03", "END:STANDARD", "END:VTIMEZONE",
]


@dataclass(frozen=True)
class InviteDetails:
    uid: str
    title: str
    day: date
    hhmm: str  # "14:00"
    duration_min: int
    location: str = ""
    description: str = ""

    @property
    def start(self) -> datetime:
        hour, minute = (int(x) for x in self.hhmm.split(":"))
        return datetime(self.day.year, self.day.month, self.day.day, hour, minute)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)

    def fingerprint(self) -> str:
        key = "|".join([self.uid, self.title, self.day.isoformat(), self.hhmm, str(self.duration_min),
                        self.location, self.description])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def escape_text(value: str) -> str:
    """RFC 5545 TEXT kaçışı."""
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str) -> str:
    """75 baytı aşan satırı katlar; çok baytlı UTF-8 karakter bölünmez."""
    out: list[str] = []
    current, size, limit = "", 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            out.append(current)
            current, size, limit = " ", 1, 75
        current += char
        size += width
    out.append(current)
    return "\r\n".join(out)


def build_ics(details: InviteDetails, sequence: int, stamp: datetime | None = None) -> str:
    stamp = (stamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fmt = "%Y%m%dT%H%M%S"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        *_VTIMEZONE,
        "BEGIN:VEVENT",
        f"UID:{details.uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{stamp.strftime(fmt)}Z",
        f"DTSTART;TZID={TZID}:{details.start.strftime(fmt)}",
        f"DTEND;TZID={TZID}:{details.end.strftime(fmt)}",
        f"SUMMARY:{escape_text(details.title)}",
    ]
    if details.location:
        lines.append(f"LOCATION:{escape_text(details.location)}")
    if details.description:
        lines.append(f"DESCRIPTION:{escape_text(details.description)}")
    lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content.encode("utf-8"))
    os.replace(temp, path)
