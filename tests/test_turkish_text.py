import pytest

from jarvis.utils.turkish_text import (
    ascii_fold,
    best_name_match,
    candidate_name_forms,
    parse_number,
    split_names,
    strip_name_suffix,
    tr_lower,
    tr_title,
    tr_upper,
)

NAMES = ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk", "Ahmet Yılmaz"]


def test_tr_lower_handles_dotted_and_dotless_i():
    assert tr_lower("IŞIK İZMİR") == "ışık izmir"
    assert tr_lower("ÇĞÖŞÜ") == "çğöşü"


def test_tr_upper_handles_dotted_and_dotless_i():
    assert tr_upper("ışık izmir") == "IŞIK İZMİR"


def test_tr_title():
    assert tr_title("ayşe yılmaz") == "Ayşe Yılmaz"
    assert tr_title("ismail ışık") == "İsmail Işık"


def test_ascii_fold():
    assert ascii_fold("Mehmet Öztürk") == "mehmet ozturk"
    assert ascii_fold("JARVIS") == "jarvis"


@pytest.mark.parametrize("word,expected", [("Ayşe'de", "Ayşe"), ("Mehmet’e", "Mehmet"), ("Ali", "Ali")])
def test_strip_name_suffix(word, expected):
    assert strip_name_suffix(word) == expected


def test_candidate_forms_without_apostrophe():
    assert "Ayşe" in candidate_name_forms("Ayşede")


@pytest.mark.parametrize("text,expected", [("15", 15), ("on beş", 15), ("yirmi", 20), ("kırk beş", 45),
                                           ("yirmi üç", 23), ("abc", None)])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_split_names():
    assert split_names("Kenan, Ayşe ve Mehmet") == ["Kenan", "Ayşe", "Mehmet"]
    assert split_names("Kenan ile Ayşe") == ["Kenan", "Ayşe"]


def test_match_unique_first_name():
    assert best_name_match("Mehmet", NAMES).name == "Mehmet Öztürk"


def test_match_full_name():
    assert best_name_match("Ahmet Yılmaz", NAMES).name == "Ahmet Yılmaz"


def test_match_with_suffix_apostrophe():
    assert best_name_match("Ayşe'de", NAMES).name == "Ayşe Yılmaz"


def test_match_with_suffix_no_apostrophe():
    assert best_name_match("Aysede", NAMES).name == "Ayşe Yılmaz"


def test_match_ascii_spelling():
    assert best_name_match("mehmet ozturk", NAMES).name == "Mehmet Öztürk"


def test_shared_surname_is_ambiguous():
    result = best_name_match("Yılmaz", NAMES)
    assert result.name is None
    assert result.is_ambiguous
    assert set(result.candidates) == {"Ayşe Yılmaz", "Ahmet Yılmaz"}


def test_unknown_name_not_matched():
    result = best_name_match("Fatma", NAMES)
    assert not result.found and not result.is_ambiguous


def test_small_typo_tolerated():
    assert best_name_match("Mehmed", NAMES).name == "Mehmet Öztürk"


def test_similar_names_not_confused():
    # "Ahmet" ve "Mehmet" birbirine benzer; doğrudan kelime eşleşmesi önceliklidir.
    assert best_name_match("Ahmet", NAMES).name == "Ahmet Yılmaz"
