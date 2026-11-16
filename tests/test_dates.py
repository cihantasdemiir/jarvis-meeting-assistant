"""Türkçe tarih ayrıştırma testleri. Taban tarih: 14 Ekim 2026 Çarşamba."""
from datetime import date

import pytest

from jarvis.utils.dates import (
    add_business_days,
    add_months,
    find_date,
    format_tr_date,
    next_weekday,
    parse_turkish_date,
    remove_span,
    weekday_of_next_week,
)

BASE = date(2026, 10, 14)  # Çarşamba


@pytest.mark.parametrize("text,expected", [
    ("bugün", date(2026, 10, 14)),
    ("yarın", date(2026, 10, 15)),
    ("yarına kadar", date(2026, 10, 15)),
    ("öbür gün", date(2026, 10, 16)),
    ("yarından sonra", date(2026, 10, 16)),
    ("haftaya", date(2026, 10, 21)),                 # PDF T-04: bugün + 7
    ("haftaya kadar", date(2026, 10, 21)),
    ("gelecek hafta", date(2026, 10, 21)),
    ("önümüzdeki haftaya kadar", date(2026, 10, 21)),
    ("gelecek cuma", date(2026, 10, 16)),
    ("gelecek cumaya kadar", date(2026, 10, 16)),
    ("önümüzdeki pazartesi", date(2026, 10, 19)),
    ("cumaya kadar", date(2026, 10, 16)),
    ("pazartesiye", date(2026, 10, 19)),
    ("çarşambaya kadar", date(2026, 10, 21)),        # bugün çarşamba -> gelecek hafta
    ("perşembe günü", date(2026, 10, 15)),
    ("haftaya cuma", date(2026, 10, 23)),
    ("haftaya pazartesi", date(2026, 10, 19)),
    ("iki gün sonra", date(2026, 10, 16)),
    ("üç gün sonra", date(2026, 10, 17)),
    ("3 gün içinde", date(2026, 10, 17)),
    ("bir hafta sonra", date(2026, 10, 21)),
    ("iki hafta içinde", date(2026, 10, 28)),
    ("bir ay sonra", date(2026, 11, 14)),
    ("3 iş günü içinde", date(2026, 10, 19)),
    ("15 Ekim", date(2026, 10, 15)),
    ("15 ekime kadar", date(2026, 10, 15)),
    ("21 Ekim 2026", date(2026, 10, 21)),
    ("21 Ekim'e kadar", date(2026, 10, 21)),
    ("1 ocak", date(2027, 1, 1)),
    ("10 ekim", date(2027, 10, 10)),                 # geçmişte kaldı -> gelecek yıl
    ("15.10.2026", date(2026, 10, 15)),
    ("15/11/26", date(2026, 11, 15)),
    ("2026-12-01", date(2026, 12, 1)),
    ("ayın 20'si", date(2026, 10, 20)),
    ("ayın 5'ine kadar", date(2026, 11, 5)),         # geçmişte kaldı -> gelecek ay
    ("ay sonu", date(2026, 10, 31)),
    ("ayın sonunda", date(2026, 10, 31)),
    ("yıl sonuna kadar", date(2026, 12, 31)),
])
def test_known_expressions(text, expected):
    assert parse_turkish_date(text, BASE) == expected


@pytest.mark.parametrize("text", [
    "yakında", "sonra bakarız", "pazar araştırması", "1.5 milyon", "%2.5 artış", "bir gün", "", "raporu hazırlasın",
    "31 şubat",
])
def test_no_false_dates(text):
    assert parse_turkish_date(text, BASE) is None


def test_this_week_assumption_is_reported():
    match = find_date("bu hafta içinde", BASE)
    assert match.value == date(2026, 10, 16)
    assert "varsayım" in match.note or "cuması" in match.note


def test_this_week_on_weekend_is_ambiguous():
    assert parse_turkish_date("bu hafta içinde", date(2026, 10, 17)) is None


def test_date_found_inside_sentence_with_span():
    text = "Mehmet raporu haftaya kadar hazırlasın"
    match = find_date(text, BASE)
    assert match.text == "haftaya kadar"
    assert remove_span(text, match.start, match.end) == "Mehmet raporu hazırlasın"


def test_longer_expression_preferred():
    assert find_date("haftaya cuma teslim", BASE).value == date(2026, 10, 23)


def test_case_insensitive_turkish():
    assert parse_turkish_date("GELECEK CUMAYA KADAR", BASE) == date(2026, 10, 16)
    assert parse_turkish_date("YARIN", BASE) == date(2026, 10, 15)


def test_helpers():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert next_weekday(BASE, 2) == date(2026, 10, 21)
    assert next_weekday(BASE, 2, include_today=True) == BASE
    assert weekday_of_next_week(date(2026, 10, 18), 0) == date(2026, 10, 19)  # pazar -> ertesi pzt
    assert add_business_days(date(2026, 10, 16), 1) == date(2026, 10, 19)    # cuma + 1 iş günü


def test_format():
    assert format_tr_date(date(2026, 10, 21)) == "21 Ekim 2026 Çarşamba"
    assert format_tr_date(date(2026, 2, 1), with_weekday=False) == "1 Şubat 2026"
    assert format_tr_date(None) == "belirlenmedi"
