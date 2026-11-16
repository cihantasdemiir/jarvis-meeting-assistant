"""Türkçe tarih ifadelerini ayrıştırma.

Göreli ifadeler her zaman bir taban tarihe göre (toplantı tarihi) hesaplanır.
Kütüphane yerine açık kurallar kullanılır; dateparser'ın Türkçe göreli
ifadelerdeki eksikleri için bkz. docs/SOURCE_AUDIT.md.

Desteklenen ifadeler (örnekler):
  bugün, yarın, öbür gün, yarından sonra
  haftaya, haftaya kadar, gelecek hafta, önümüzdeki hafta      -> +7 gün (PDF T-04)
  cumaya, cuma günü, gelecek cuma, önümüzdeki pazartesi        -> o günün ilk gelecek tarihi
  (yalın "pazar" gibi kelimeler "pazar araştırması" ile karışmasın diye tarih sayılmaz)
  haftaya cuma, haftaya pazartesi                              -> bir sonraki haftanın o günü
  bu hafta içinde, hafta sonuna kadar                          -> bu haftanın cuması (varsayım)
  iki gün sonra, 3 gün içinde, bir hafta sonra, 2 ay sonra
  3 iş günü içinde                                             -> hafta sonu atlanır
  15 ekim, 15 Ekim 2026, 15 ekime kadar, 15.10.2026, 15/10/2026, 2026-10-15
  ayın 20'si, ay sonu, ayın sonunda, yıl sonu

Belirsiz kalan ifadeler (ör. "yakında", "sonra") tarih olarak yorumlanmaz.
Bu durumda çağıran kod kullanıcıdan teyit ister.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from .turkish_text import NUMBER_WORDS, parse_number, tr_lower

MONTHS: dict[str, int] = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
}
MONTH_NAMES = {number: name for name, number in MONTHS.items()}

WEEKDAYS: dict[str, int] = {
    "pazartesi": 0, "salı": 1, "çarşamba": 2, "perşembe": 3, "cumartesi": 5, "cuma": 4, "pazar": 6,
}
WEEKDAY_NAMES = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# Sözcük sınırı: Türkçe harfler \w kapsamındadır.
_B = r"(?<![\w])"
_E = r"(?![\w])"
_NUM = "(?:\\d+|" + "|".join(sorted((re.escape(w) for w in NUMBER_WORDS if NUMBER_WORDS[w] > 0),
                                    key=len, reverse=True)) + ")"
# Gün adlarından sonra gelebilecek ekler: cumaya, pazartesiye, pazara, cuma günü, cuma gününe
_DAY_SUFFIX = r"(?:'?(?:ya|ye|a|e|ne|na|dan|den|ya kadar|ye kadar))?(?: günü(?:ne)?)?"
_WEEKDAY_RE = "(?P<day>" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + ")" + _DAY_SUFFIX
_BARE_WEEKDAY_RE = ("(?P<day>" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + ")"
                    + r"(?:'?(?:ya|ye|a|e|ne|na)(?: kadar)?| günü(?:ne)?| kadar)")
_MONTH_RE = "(?P<month>" + "|".join(MONTHS) + ")" + r"(?:'?[a-zçğıöşü]{0,4})?"


@dataclass(frozen=True)
class DateMatch:
    """Metinde bulunan tarih ifadesi."""

    value: date
    start: int
    end: int
    text: str
    note: str = ""  # yapılan varsayım (kullanıcıya okunur)


# ------------------------------------------------------------------ yardımcılar
def add_months(base: date, months: int) -> date:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_weekday(base: date, weekday: int, include_today: bool = False) -> date:
    """Tabandan sonraki ilk `weekday` günü (bugün o günse bir hafta sonrası)."""
    delta = (weekday - base.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return base + timedelta(days=delta)


def weekday_of_next_week(base: date, weekday: int) -> date:
    """"Haftaya cuma": bir sonraki takvim haftasının (Pzt-Paz) ilgili günü."""
    next_monday = base - timedelta(days=base.weekday()) + timedelta(days=7)
    return next_monday + timedelta(days=weekday)


def add_business_days(base: date, days: int) -> date:
    current = base
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def end_of_month(base: date) -> date:
    return date(base.year, base.month, calendar.monthrange(base.year, base.month)[1])


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _future_date(base: date, month: int, day: int, year: int | None) -> date | None:
    """Yıl verilmemişse geçmişte kalan tarih bir sonraki yıla taşınır."""
    if year is not None:
        return _safe_date(year, month, day)
    candidate = _safe_date(base.year, month, day)
    if candidate is not None and candidate < base:
        candidate = _safe_date(base.year + 1, month, day)
    return candidate


def _num(text: str) -> int | None:
    return parse_number(text)


# ------------------------------------------------------------------ kurallar
Resolver = Callable[[re.Match, date], "tuple[date | None, str]"]


def _r_today(m: re.Match, base: date):
    return base, ""


def _r_tomorrow(m: re.Match, base: date):
    return base + timedelta(days=1), ""


def _r_day_after(m: re.Match, base: date):
    return base + timedelta(days=2), ""


def _r_next_week(m: re.Match, base: date):
    return base + timedelta(days=7), ""


def _r_next_week_day(m: re.Match, base: date):
    return weekday_of_next_week(base, WEEKDAYS[m.group("day")]), ""


def _r_weekday(m: re.Match, base: date):
    return next_weekday(base, WEEKDAYS[m.group("day")]), ""


def _r_this_week(m: re.Match, base: date):
    friday = next_weekday(base, 4, include_today=True)
    if base.weekday() > 4:  # hafta sonundaysak "bu hafta" belirsizdir
        return None, ""
    return friday, "bu haftanın cuması olarak alındı"


def _r_relative(m: re.Match, base: date):
    count = _num(m.group("n"))
    if count is None:
        return None, ""
    unit = m.group("unit")
    if unit.startswith("iş"):
        return add_business_days(base, count), ""
    if unit.startswith("gün"):
        return base + timedelta(days=count), ""
    if unit.startswith("hafta"):
        return base + timedelta(days=7 * count), ""
    if unit.startswith("ay"):
        return add_months(base, count), ""
    return None, ""


def _r_month_end(m: re.Match, base: date):
    return end_of_month(base), ""


def _r_year_end(m: re.Match, base: date):
    return date(base.year, 12, 31), ""


def _r_day_of_month(m: re.Match, base: date):
    day = int(m.group("d"))
    candidate = _safe_date(base.year, base.month, day)
    if candidate is None or candidate < base:
        following = add_months(date(base.year, base.month, 1), 1)
        candidate = _safe_date(following.year, following.month, day)
    return candidate, ""


def _r_day_month(m: re.Match, base: date):
    year = int(m.group("y")) if m.group("y") else None
    return _future_date(base, MONTHS[m.group("month")], int(m.group("d")), year), ""


def _r_numeric(m: re.Match, base: date):
    year_raw = m.group("y")
    year = None
    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
    return _future_date(base, int(m.group("m")), int(m.group("d")), year), ""


def _r_iso(m: re.Match, base: date):
    return _safe_date(int(m.group("y")), int(m.group("m")), int(m.group("d"))), ""


_RULES: list[tuple[re.Pattern, Resolver]] = [
    (re.compile(rf"{_B}(?P<y>\d{{4}})-(?P<m>\d{{1,2}})-(?P<d>\d{{1,2}}){_E}"), _r_iso),
    # Yılsız "1.5" gibi sayılar tarih sanılmasın diye nokta/eğik çizgili biçimde yıl zorunludur.
    (re.compile(rf"{_B}(?P<d>\d{{1,2}})[./](?P<m>\d{{1,2}})[./](?P<y>\d{{2,4}}){_E}"), _r_numeric),
    (re.compile(rf"{_B}(?P<d>\d{{1,2}})\s*{_MONTH_RE}(?:\s+(?P<y>\d{{4}}))?{_E}"), _r_day_month),
    (re.compile(rf"{_B}ayın (?P<d>\d{{1,2}})(?:'?[a-zçğıöşü]{{0,6}})?{_E}"), _r_day_of_month),
    (re.compile(rf"{_B}(?:ay sonu(?:na)?|ayın sonu(?:na|nda)?|ay bitmeden){_E}"), _r_month_end),
    (re.compile(rf"{_B}(?:yıl sonu(?:na)?|yılın sonu(?:na|nda)?|sene sonu(?:na)?){_E}"), _r_year_end),
    (re.compile(rf"{_B}(?P<n>{_NUM}) (?P<unit>iş günü|gün|hafta|ay)\s+(?:sonra(?:ya)?|içinde|içerisinde){_E}"),
     _r_relative),
    (re.compile(rf"{_B}haftaya {_WEEKDAY_RE}{_E}"), _r_next_week_day),
    (re.compile(rf"{_B}(?:gelecek|önümüzdeki|bu|ilk) {_WEEKDAY_RE}{_E}"), _r_weekday),
    (re.compile(rf"{_B}(?:haftaya|gelecek hafta(?:ya)?|önümüzdeki hafta(?:ya)?|bir hafta sonra){_E}"),
     _r_next_week),
    (re.compile(rf"{_B}(?:bu hafta(?: içinde| içerisinde| sonuna)?|hafta sonuna|hafta bitmeden){_E}"), _r_this_week),
    # Yalın gün adı ("pazar araştırması") tarih sayılmaz; ek veya "günü/kadar" gerekir: "cumaya", "pazartesi günü".
    (re.compile(rf"{_B}{_BARE_WEEKDAY_RE}{_E}"), _r_weekday),
    (re.compile(rf"{_B}(?:yarından sonra|öbür gün(?:e)?|ertesi gün){_E}"), _r_day_after),
    (re.compile(rf"{_B}(?:yarın(?:a)?){_E}"), _r_tomorrow),
    (re.compile(rf"{_B}(?:bugün(?:e)?|bu akşam(?:a)?|gün sonuna|gün bitmeden){_E}"), _r_today),
]

# Tarih ifadesinden sonra gelen "kadar", "'a kadar" gibi kalıplar açıklamadan da silinir.
_TRAILING_UNTIL = re.compile(r"^(?:'?(?:a|e|ya|ye|na|ne))?\s*(?:kadar|tarihine kadar|itibarıyla)?(?=\W|$)")


def find_date(text: str, base: date) -> DateMatch | None:
    """Metindeki ilk geçerli tarih ifadesini bulur."""
    lowered = tr_lower(text)
    if len(lowered) != len(text):  # nadir Unicode durumunda konum kayması olmasın
        text = lowered
    candidates: list[DateMatch] = []
    for pattern, resolver in _RULES:
        for match in pattern.finditer(lowered):
            value, note = resolver(match, base)
            if value is None:
                continue
            end = match.end()
            tail = _TRAILING_UNTIL.match(lowered[end:])
            if tail and tail.group(0).strip():
                end += tail.end()
            candidates.append(DateMatch(value, match.start(), end, text[match.start():end].strip(), note))
    if not candidates:
        return None
    # Önce en erken başlayan, eşitse en uzun ifade (ör. "haftaya cuma" > "cuma")
    candidates.sort(key=lambda c: (c.start, -(c.end - c.start)))
    return candidates[0]


def parse_turkish_date(text: str, base: date) -> date | None:
    """Tarih ifadesini tarihe çevirir; bulunamazsa None."""
    match = find_date(text, base)
    return match.value if match else None


def remove_span(text: str, start: int, end: int) -> str:
    """Metinden bir aralığı çıkarır ve boşlukları düzenler."""
    result = (text[:start] + " " + text[end:]).strip()
    result = re.sub(r"\s+([,.;:])", r"\1", result)
    return re.sub(r"\s+", " ", result).strip(" ,;:")


def format_tr_date(value: date | None, with_weekday: bool = True) -> str:
    """date(2026, 10, 21) -> "21 Ekim 2026 Çarşamba"."""
    if value is None:
        return "belirlenmedi"
    month = MONTH_NAMES[value.month].capitalize()
    text = f"{value.day} {month} {value.year}"
    if with_weekday:
        text += f" {WEEKDAY_NAMES[value.weekday()]}"
    return text


def format_tr_short(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


# ====================================================================== saat (Faz 6: takvim daveti)
@dataclass(frozen=True)
class TimeMatch:
    """Metinde bulunan saat ifadesi. `ambiguous`: sabah mı öğleden sonra mı belli değil; `error`: geçersiz."""

    hour: int
    minute: int
    start: int
    end: int
    text: str
    ambiguous: bool = False
    error: str = ""

    @property
    def hhmm(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


_ONES = {w: n for w, n in NUMBER_WORDS.items() if 0 < n < 10 and " " not in w}
_HOUR_WORDS = {**_ONES, "on": 10, "yirmi": 20, **{f"on {w}": 10 + n for w, n in _ONES.items()},
               **{f"yirmi {w}": 20 + n for w, n in _ONES.items() if n <= 3}}
_HOUR_WORD_RE = "|".join(sorted((re.escape(w) for w in _HOUR_WORDS), key=len, reverse=True))
_PERIOD = r"(?P<period>sabah|öğleden önce|öğle(?:n|yin)?|öğleden sonra|ikindi|akşam|gece)"
_TIME_SUFFIX = r"(?:'?(?:de|da|te|ta|e|a|ye|ya))?"
_TIME_RULES = [
    # "saat 14.00", "saat 14:30'da", "öğleden sonra saat 2", "saat 9 buçukta"
    re.compile(rf"{_B}(?:{_PERIOD}\s+)?saat\s+(?P<h>\d{{1,2}})(?:[.:](?P<m>\d{{1,2}}))?{_TIME_SUFFIX}"
               rf"(?P<half>\s+buçuk(?:ta)?)?(?:\s+{_PERIOD.replace('period', 'period2')})?{_E}"),
    # "saat on dört", "saat iki buçukta"
    re.compile(rf"{_B}(?:{_PERIOD}\s+)?saat\s+(?P<hw>{_HOUR_WORD_RE}){_TIME_SUFFIX}"
               rf"(?P<half>\s+buçuk(?:ta)?)?(?:\s+{_PERIOD.replace('period', 'period2')})?{_E}"),
    # "14.00'te", "14:30" (tarih "15.10.2026" ile karışmasın diye ardından nokta/rakam gelmez)
    re.compile(rf"{_B}(?:{_PERIOD}\s+)?(?P<h>\d{{1,2}})[:.](?P<m>\d{{2}}){_TIME_SUFFIX}(?![\w./])"),
]
_PM = {"öğleden sonra", "ikindi", "akşam"}


def find_time(text: str) -> TimeMatch | None:
    """Metindeki ilk saat ifadesini bulur. 1–6 arası saat, sabah/öğleden sonra denmezse belirsiz sayılır
    (09.00 ile 21.00 karışmasın diye uydurulmaz); 7–11 sabah, 12–23 olduğu gibi kabul edilir."""
    lowered = tr_lower(text)
    best: TimeMatch | None = None
    for pattern in _TIME_RULES:
        match = pattern.search(lowered)
        if match is None or (best is not None and best.start <= match.start()):
            continue
        groups = match.groupdict()
        period = groups.get("period") or groups.get("period2")
        hour = int(groups["h"]) if groups.get("h") else _HOUR_WORDS.get(groups.get("hw") or "", -1)
        minute = int(groups["m"]) if groups.get("m") else (30 if groups.get("half") else 0)
        span = (match.start(), match.end(), text[match.start():match.end()].strip())
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or (groups.get("m") and len(groups["m"]) != 2):
            best = TimeMatch(0, 0, *span, error=f"'{span[2]}' geçerli bir saat değil")
            continue
        ambiguous = False
        if period in _PM and hour < 12:
            hour += 12
        elif period == "gece" and 7 <= hour < 12:
            hour += 12
        elif period is None and 1 <= hour <= 6:
            ambiguous = True
        elif hour == 0 and period is None and minute == 0 and not groups.get("m"):
            ambiguous = True
        best = TimeMatch(hour, minute, *span, ambiguous=ambiguous)
    return best


def format_date_time(value: date | None, hhmm: str = "") -> str:
    """"21 Ekim 2026 Çarşamba, saat 14.00" (saat yoksa yalnızca tarih)."""
    text = format_tr_date(value)
    return f"{text}, saat {hhmm.replace(':', '.')}" if value and hhmm else text
