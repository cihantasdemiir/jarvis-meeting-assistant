"""CommandParser testleri (PDF hedefi: en az 50 test).

Her parametre satırı ayrı bir pytest testi olarak sayılır.
"""
import pytest

from jarvis.voice.command_parser import CommandParser, IntentType as I

ALIASES = ["jarvıs", "carvis", "cervis", "çarvis", "jarves", "jarviz"]


@pytest.fixture(scope="module")
def parser() -> CommandParser:
    return CommandParser("jarvis", ALIASES)


# (girdi, beklenen niyet)
INTENT_CASES = [
    # Toplantı yaşam döngüsü
    ("Jarvis, toplantıyı başlat", I.START_MEETING),
    ("jarvis toplantıyı başlat", I.START_MEETING),
    ("JARVIS TOPLANTIYI BAŞLAT", I.START_MEETING),
    ("Jarvis yeni toplantıyı aç", I.START_MEETING),
    ("Jarvis toplantıya başlayalım", I.START_MEETING),
    ("Jarvis, toplantıyı bitir.", I.END_MEETING),
    ("Jarvis toplantıyı kapat", I.END_MEETING),
    ("Jarvis toplantıyı sonlandır", I.END_MEETING),
    ("Jarvis toplantı bitti", I.END_MEETING),
    # Yoklama
    ("Jarvis yoklama al", I.ATTENDANCE),
    ("Jarvis kim katılmadı", I.ATTENDANCE),
    ("Jarvis katılım durumu", I.ATTENDANCE),
    ("Jarvis Ahmet katıldı", I.MARK_PRESENT),
    ("Jarvis, katılımcılar: Kenan, Ayşe, Mehmet", I.MARK_PRESENT_MANY),
    ("Jarvis katılımcılar Kenan Ayşe ve Mehmet", I.MARK_PRESENT_MANY),  # virgülsüz STT çıktısı
    ("Jarvis Ayşe ve Mehmet geldiler", I.MARK_PRESENT_MANY),
    # Gündem
    ("Jarvis, gündemi oku", I.READ_AGENDA),
    ("Jarvis gündemde neler var", I.READ_AGENDA),
    ("Jarvis sıradaki madde", I.NEXT_ITEM),
    ("Jarvis sonraki madde", I.NEXT_ITEM),
    ("Jarvis bir sonraki konuya geçelim", I.NEXT_ITEM),
    ("Jarvis sıradaki konuya geç", I.NEXT_ITEM),
    ("Jarvis önceki madde", I.PREVIOUS_ITEM),
    ("Jarvis bir önceki maddeye dön", I.PREVIOUS_ITEM),
    ("Jarvis maddeyi ertele", I.SKIP_ITEM),
    ("Jarvis bu konuyu erteleyelim", I.SKIP_ITEM),
    ("Jarvis gündeme ekle: Bütçe, 15 dakika", I.ADD_AGENDA_ITEM),
    # Önceki toplantı ve görevler
    ("Jarvis, önceki toplantıyı özetle", I.SUMMARIZE_PREVIOUS),
    ("Jarvis geçen toplantıda ne oldu", I.SUMMARIZE_PREVIOUS),
    ("Jarvis açık görevleri oku", I.OPEN_ACTIONS),
    ("Jarvis devreden görevler", I.OPEN_ACTIONS),
    # Konuşmacı
    ("Jarvis, söz Ayşe'de", I.SET_SPEAKER),
    ("Jarvis söz aysede", I.SET_SPEAKER),
    ("Jarvis Mehmet konuşuyor", I.SET_SPEAKER),
    ("Jarvis sözü Ahmet'e ver", I.SET_SPEAKER),
    ("Jarvis sözü bırak", I.RELEASE_SPEAKER),
    ("Jarvis kim konuşmadı", I.SILENT_PARTICIPANTS),
    ("Jarvis konuşmayanlar kimler", I.SILENT_PARTICIPANTS),
    # Kayıtlar
    ("Jarvis, karar: Yeni üretim planı pazartesi uygulanacak", I.ADD_DECISION),
    ("Jarvis karar yeni plan onaylandı", I.ADD_DECISION),
    ("Jarvis, görev: Mehmet raporu haftaya kadar hazırlasın", I.ADD_ACTION),
    ("Jarvis görev Ayşe sunumu cumaya kadar bitirsin", I.ADD_ACTION),
    ("Jarvis görev tamamlandı: rapor", I.COMPLETE_ACTION),
    ("Jarvis rapor görevi tamamlandı", I.COMPLETE_ACTION),
    ("Jarvis not: tedarikçi ile görüşülecek", I.ADD_NOTE),
    ("Jarvis not al müşteri ziyareti ertelendi", I.ADD_NOTE),
    ("Jarvis otopark: ofis taşınması", I.ADD_PARKING),
    ("Jarvis oylama: bütçe artışı lehte 5 aleyhte 2", I.VOTE),
    # Süre ve tutanak
    ("Jarvis süre durumu", I.TIME_STATUS),
    ("Jarvis ne kadar süre kaldı", I.TIME_STATUS),
    ("Jarvis kalan süre", I.TIME_STATUS),
    ("Jarvis sonraki toplantı 21 ekim", I.NEXT_MEETING),
    ("Jarvis tutanağı gönder", I.SEND_MINUTES),
    ("Jarvis tutanağı tekrar gönder", I.SEND_MINUTES),
    # Genel
    ("Jarvis yardım", I.HELP),
    ("Jarvis ne yapabilirsin", I.HELP),
    ("Jarvis çık", I.EXIT),
    # Uyandırma kelimesi varyantları
    ("Jarvıs gündemi oku", I.READ_AGENDA),
    ("carvis gündemi oku", I.READ_AGENDA),
    ("Hey Jarvis gündemi oku", I.READ_AGENDA),
    ("!gündemi oku", I.READ_AGENDA),
    ("Jarvis: gündemi oku", I.READ_AGENDA),
    # Anlaşılmayan komutlar asla başka komuta dönüşmez
    ("Jarvis bugün hava nasıl", I.UNKNOWN),
    ("Jarvis", I.UNKNOWN),
    ("Jarvis toplantı", I.UNKNOWN),
    ("Jarvis görevler", I.UNKNOWN),
]


@pytest.mark.parametrize("text,expected", INTENT_CASES)
def test_intent_detection(parser, text, expected):
    intent = parser.parse(text)
    assert intent is not None, f"Komut algılanmadı: {text}"
    assert intent.type == expected


@pytest.mark.parametrize("text", [
    "Bu hafta iki hat bakımda olacak.",
    "Toplantıyı başlatalım mı sizce?",
    "Karar vermeden önce rakamlara bakalım.",
    "Jarvis'in raporunu okudum mu bilmiyorum",  # Jarvis cümlenin başında değil, ekli: "Jarvis'in"
    "Ben de jarvis'e sormuştum",
    "Jarvis'cim gündemi oku",  # ekli hitap komut sayılmaz; transkripte gider
    "Jarvis'in önerisi mantıklı",
    "",
])
def test_non_commands_return_none(parser, text):
    assert parser.parse(text) is None


def test_start_with_title_keeps_original_case(parser):
    intent = parser.parse("Jarvis toplantıyı başlat: Üretim Planlama İstişaresi")
    assert intent.get("title") == "Üretim Planlama İstişaresi"


def test_mark_present_many_names(parser):
    intent = parser.parse("Jarvis, katılımcılar: Kenan, Ayşe ve Mehmet.")
    assert intent.get("names") == ["Kenan", "Ayşe", "Mehmet"]


def test_set_speaker_strips_suffix(parser):
    assert parser.parse("Jarvis, söz Ayşe'de").get("speaker") == "Ayşe"
    assert parser.parse("Jarvis söz Mehmet Öztürk'te").get("speaker") == "Mehmet Öztürk"
    assert parser.parse("Jarvis sözü Ahmet'e ver").get("speaker") == "Ahmet"


def test_set_speaker_without_apostrophe_keeps_raw_word(parser):
    # Kök çıkarma isim listesiyle eşleştirilirken yapılır (best_name_match).
    assert parser.parse("Jarvis söz aysede").get("speaker") == "aysede"


def test_decision_text_original_case(parser):
    intent = parser.parse("Jarvis, karar: Yeni üretim planı Pazartesi uygulanacak.")
    assert intent.get("text") == "Yeni üretim planı Pazartesi uygulanacak"


def test_action_text(parser):
    intent = parser.parse("Jarvis, görev: Mehmet raporu haftaya kadar hazırlasın.")
    assert intent.get("text") == "Mehmet raporu haftaya kadar hazırlasın"


def test_complete_action_query(parser):
    assert parser.parse("Jarvis görev tamamlandı: rapor").get("query") == "rapor"
    assert parser.parse("Jarvis bütçe raporu görevi tamamlandı").get("query") == "bütçe raporu"


@pytest.mark.parametrize("text,title,minutes", [
    ("Jarvis gündeme ekle: Bütçe, 15 dakika", "Bütçe", 15),
    ("Jarvis gündeme ekle Yeni personel oryantasyonu 5 dakikalık", "Yeni personel oryantasyonu", 5),
    ("Jarvis gündeme ekle: Risk analizi, on beş dakika", "Risk analizi", 15),
    ("Jarvis gündeme ekle: Satış hedefleri", "Satış hedefleri", None),
])
def test_add_agenda_item_params(parser, text, title, minutes):
    intent = parser.parse(text)
    assert intent.type == I.ADD_AGENDA_ITEM
    assert intent.get("title") == title
    assert intent.get("minutes") == minutes


@pytest.mark.parametrize("text,topic,yes,no,abstain", [
    ("Jarvis oylama: bütçe artışı lehte 5 aleyhte 2", "bütçe artışı", 5, 2, None),
    ("Jarvis oylama yeni vardiya lehte beş aleyhte iki çekimser bir", "yeni vardiya", 5, 2, 1),
    ("Jarvis oylama: Ek mesai, lehte 4 aleyhte 4", "Ek mesai", 4, 4, None),
])
def test_vote_params(parser, text, topic, yes, no, abstain):
    intent = parser.parse(text)
    assert intent.type == I.VOTE
    actual = (intent.get("topic"), intent.get("yes"), intent.get("no"), intent.get("abstain"))
    assert actual == (topic, yes, no, abstain)


def test_next_meeting_date_text(parser):
    assert parser.parse("Jarvis sonraki toplantı 21 Ekim").get("date_text") == "21 Ekim"


def test_note_and_parking_text(parser):
    assert parser.parse("Jarvis not: Tedarikçi ile görüşülecek").get("text") == "Tedarikçi ile görüşülecek"
    assert parser.parse("Jarvis not al Müşteri ziyareti").get("text") == "Müşteri ziyareti"
    assert parser.parse("Jarvis otopark: Ofis taşınması").get("text") == "Ofis taşınması"


def test_parse_without_wake_when_not_required(parser):
    assert parser.parse("gündemi oku", require_wake=False).type == I.READ_AGENDA


def test_custom_wake_word():
    custom = CommandParser("asistan", [])
    assert custom.parse("Asistan gündemi oku").type == I.READ_AGENDA
    assert custom.parse("Jarvis gündemi oku") is None


def test_turkish_capital_i_in_command(parser):
    # "İ" ve "I" harfleri doğru küçültülmeli
    assert parser.parse("JARVIS SIRADAKİ MADDE").type == I.NEXT_ITEM
    assert parser.parse("JARVIS ÖNCEKİ TOPLANTIYI ÖZETLE").type == I.SUMMARIZE_PREVIOUS
