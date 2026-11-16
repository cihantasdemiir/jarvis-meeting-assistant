"""Görev metninden sorumlu / son tarih / açıklama çıkarımı."""
from datetime import date

import pytest

from jarvis.meeting.action_extractor import extract_action

NAMES = ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk", "Ahmet Yılmaz"]
BASE = date(2026, 10, 14)


def test_t04_owner_and_plus_seven_days():
    draft = extract_action("Mehmet raporu haftaya kadar hazırlasın", NAMES, BASE)
    assert draft.owner == "Mehmet Öztürk"
    assert draft.due_date == date(2026, 10, 21)
    assert draft.description == "raporu hazırlasın"
    assert draft.missing == []


@pytest.mark.parametrize("text,owner,due,description", [
    ("Mehmet raporu gelecek cumaya kadar hazırlasın", "Mehmet Öztürk", date(2026, 10, 16), "raporu hazırlasın"),
    ("Mehmet Öztürk bütçe tablosunu 21 Ekim'e kadar göndersin", "Mehmet Öztürk", date(2026, 10, 21),
     "bütçe tablosunu göndersin"),
    ("ayşe sunumu cumaya kadar bitirsin", "Ayşe Yılmaz", date(2026, 10, 16), "sunumu bitirsin"),
    ("raporu Mehmet hazırlasın", "Mehmet Öztürk", None, "raporu hazırlasın"),
    ("Bütçe raporu hazırlansın, sorumlu: Ahmet, iki gün içinde", "Ahmet Yılmaz", date(2026, 10, 16),
     "Bütçe raporu hazırlansın"),
    ("Ahmet Yılmaz bakım planını yarın paylaşsın", "Ahmet Yılmaz", date(2026, 10, 15), "bakım planını paylaşsın"),
])
def test_extraction_cases(text, owner, due, description):
    draft = extract_action(text, NAMES, BASE)
    assert (draft.owner, draft.due_date, draft.description) == (owner, due, description)


def test_ambiguous_surname_is_not_assigned():
    draft = extract_action("Yılmaz sunumu hazırlasın", NAMES, BASE)
    assert draft.owner == ""
    assert draft.owner_ambiguous
    assert set(draft.owner_candidates) == {"Ayşe Yılmaz", "Ahmet Yılmaz"}


def test_unknown_person_is_not_invented():
    draft = extract_action("Fatma raporu hazırlasın", NAMES, BASE)
    assert draft.owner == "" and not draft.owner_candidates
    assert draft.description == "Fatma raporu hazırlasın"
    assert draft.missing == ["sorumlu", "son tarih"]


def test_no_owner_no_date():
    draft = extract_action("tedarikçiyle görüşülsün", NAMES, BASE)
    assert draft.owner == "" and draft.due_date is None
    assert draft.description == "tedarikçiyle görüşülsün"


def test_lowercase_common_word_not_taken_as_name():
    # "kenan" küçük harfle ve cümle ortasında; isim sayılmaz (yanlış atama riskine karşı)
    draft = extract_action("raporu kenan değil ekip hazırlasın", NAMES, BASE)
    assert draft.owner == ""


def test_this_week_assumption_note():
    draft = extract_action("Ayşe bu hafta içinde dönüş yapsın", NAMES, BASE)
    assert draft.due_date == date(2026, 10, 16)
    assert draft.date_note


def test_empty_participant_list():
    draft = extract_action("Mehmet raporu yarın hazırlasın", [], BASE)
    assert draft.owner == "" and draft.due_date == date(2026, 10, 15)
