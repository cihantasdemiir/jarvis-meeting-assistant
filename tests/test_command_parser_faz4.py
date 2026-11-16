"""Faz 4: ses tanıma çıktısına özgü ayrıştırma (noktalama yok, hitap cümle ortasında olabilir)."""
import pytest

from jarvis.voice.command_parser import CommandParser, IntentType as I

PARSER = CommandParser("jarvis", ["jarvıs", "carvis", "javis"])


@pytest.mark.parametrize("text,before,command", [
    ("böyle karar verdik Jarvis sıradaki madde", "böyle karar verdik", "Jarvis sıradaki madde"),
    ("tamam o zaman carvis gündemi oku", "tamam o zaman", "carvis gündemi oku"),
    ("evet, Jarvis, süre durumu", "evet", "Jarvis, süre durumu"),
])
def test_split_embedded_command(text, before, command):
    assert PARSER.split_embedded_command(text) == (before, command)


@pytest.mark.parametrize("text", ["Jarvis'in önerisi iyiydi", "bugün hava güzel", "Jarvis gündemi oku"])
def test_no_embedded_command(text):
    assert PARSER.split_embedded_command(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Jarvis not tedarikçi ile görüşülecek", "tedarikçi ile görüşülecek"),
    ("jarvis not al müşteri ziyareti", "müşteri ziyareti"),
    ("Jarvis not: bütçe revize edilecek", "bütçe revize edilecek"),
])
def test_note_without_colon(text, expected):
    intent = PARSER.parse(text)
    assert intent.type == I.ADD_NOTE and intent.get("text") == expected


@pytest.mark.parametrize("text,expected", [
    ("javis gündemi oku", I.READ_AGENDA),
    ("jarvis görev mehmet raporu haftaya kadar hazırlasın", I.ADD_ACTION),
    ("jarvis oylama bütçe artışı lehte beş aleyhte iki", I.VOTE),
    ("jarvis gündeme ekle bütçe on beş dakika", I.ADD_AGENDA_ITEM),
])
def test_typical_google_output_without_punctuation(text, expected):
    assert PARSER.parse(text).type == expected
