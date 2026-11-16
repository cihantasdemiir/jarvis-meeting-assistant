"""Faz 2'de eklenen komutlar ve parametreler."""
import pytest

from jarvis.voice.command_parser import CommandParser, IntentType as I


@pytest.fixture(scope="module")
def parser() -> CommandParser:
    return CommandParser("jarvis", ["jarvıs", "carvis"])


@pytest.mark.parametrize("text,expected", [
    ("Jarvis görev iptal: rapor", I.CANCEL_ACTION),
    ("Jarvis görevi iptal et sunum", I.CANCEL_ACTION),
    ("Jarvis sorumlu: Ayşe", I.SET_ACTION_OWNER),
    ("Jarvis görevin sorumlusu Mehmet Öztürk", I.SET_ACTION_OWNER),
    ("Jarvis son tarih: 21 ekim", I.SET_ACTION_DUE),
    ("Jarvis görevin son tarihi haftaya", I.SET_ACTION_DUE),
    ("Jarvis geri al", I.UNDO_LAST),
    ("Jarvis son kaydı sil", I.UNDO_LAST),
    ("Jarvis kim konuşuyor", I.SPEAKER_STATS),
    ("Jarvis konuşma süreleri", I.SPEAKER_STATS),
    ("Jarvis kim ne kadar konuştu", I.SPEAKER_STATS),
    ("Jarvis görevler", I.UNKNOWN),
    ("Jarvis görev tamamlandı: 2", I.COMPLETE_ACTION),
])
def test_new_intents(parser, text, expected):
    assert parser.parse(text).type == expected


def test_owner_param(parser):
    assert parser.parse("Jarvis sorumlu: Ayşe Yılmaz").get("name") == "Ayşe Yılmaz"


def test_due_param(parser):
    assert parser.parse("Jarvis son tarih: 21 Ekim").get("date_text") == "21 Ekim"


def test_cancel_not_parsed_as_new_action(parser):
    intent = parser.parse("Jarvis görev iptal: rapor")
    assert intent.type == I.CANCEL_ACTION and intent.get("query") == "rapor"


def test_who_is_speaking_not_a_speaker_named_kim(parser):
    assert parser.parse("Jarvis kim konuşuyor").type != I.SET_SPEAKER
