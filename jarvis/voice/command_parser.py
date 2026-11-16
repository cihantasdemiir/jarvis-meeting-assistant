"""Türkçe komut ayrıştırıcı.

Metni (klavye veya STT çıktısı) bir `Intent` nesnesine dönüştürür.
Yöntem deterministiktir: açık regex kuralları kullanılır, LLM kullanılmaz.

Kurallar:
  * Komut, uyandırma kelimesiyle ("Jarvis" veya yanlış tanıma varyantları)
    başlamalıdır. Uyandırma kelimesi yoksa `parse()` None döndürür; bu metin
    normal toplantı konuşmasıdır ve transkripte yazılır.
  * Uyandırma kelimesi var ama komut anlaşılamadıysa UNKNOWN döner; hiçbir
    zaman tahminle başka bir komuta dönüştürülmez.
  * Parametreler (isim, başlık, karar metni) kullanıcının orijinal yazımıyla
    alınır; yalnızca eşleştirme küçük harfle yapılır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..utils.turkish_text import (
    NUMBER_WORDS,
    ascii_fold,
    normalize_spaces,
    parse_number,
    split_names,
    strip_name_suffix,
    tr_lower,
)


class IntentType(str, Enum):
    START_MEETING = "START_MEETING"
    END_MEETING = "END_MEETING"
    ATTENDANCE = "ATTENDANCE"
    MARK_PRESENT = "MARK_PRESENT"
    MARK_PRESENT_MANY = "MARK_PRESENT_MANY"
    READ_AGENDA = "READ_AGENDA"
    ADD_AGENDA_ITEM = "ADD_AGENDA_ITEM"
    NEXT_ITEM = "NEXT_ITEM"
    PREVIOUS_ITEM = "PREVIOUS_ITEM"
    SKIP_ITEM = "SKIP_ITEM"
    SUMMARIZE_PREVIOUS = "SUMMARIZE_PREVIOUS"
    OPEN_ACTIONS = "OPEN_ACTIONS"
    SET_SPEAKER = "SET_SPEAKER"
    RELEASE_SPEAKER = "RELEASE_SPEAKER"
    SILENT_PARTICIPANTS = "SILENT_PARTICIPANTS"
    ADD_DECISION = "ADD_DECISION"
    ADD_ACTION = "ADD_ACTION"
    COMPLETE_ACTION = "COMPLETE_ACTION"
    CANCEL_ACTION = "CANCEL_ACTION"
    SET_ACTION_OWNER = "SET_ACTION_OWNER"
    SET_ACTION_DUE = "SET_ACTION_DUE"
    UNDO_LAST = "UNDO_LAST"
    SPEAKER_STATS = "SPEAKER_STATS"
    ADD_NOTE = "ADD_NOTE"
    ADD_PARKING = "ADD_PARKING"
    VOTE = "VOTE"
    TIME_STATUS = "TIME_STATUS"
    NEXT_MEETING = "NEXT_MEETING"
    CREATE_INVITE = "CREATE_INVITE"  # Faz 6: sonraki toplantının .ics daveti (dosya; e-posta göndermez)
    ATTACH_INVITE = "ATTACH_INVITE"  # Faz 6: daveti tutanak e-postasına ekle (göndermez)
    EXPORT_TASKS = "EXPORT_TASKS"  # Faz 6: görevleri .xlsx dosyasına aktar (yalnızca okur)
    SEND_MINUTES = "SEND_MINUTES"
    CREATE_MINUTES = "CREATE_MINUTES"
    # Faz 5: yerel LLM ile güvenli çalışma (onay ve taslaklar yalnızca kurallı ayrıştırıcıdan gelir)
    CONFIRM = "CONFIRM"
    REJECT_PENDING = "REJECT_PENDING"
    ANALYZE_MEETING = "ANALYZE_MEETING"
    LIST_DRAFTS = "LIST_DRAFTS"
    APPROVE_DRAFT = "APPROVE_DRAFT"
    REJECT_DRAFT = "REJECT_DRAFT"
    READ_AI_SUMMARY = "READ_AI_SUMMARY"
    ASK_HISTORY = "ASK_HISTORY"
    HELP = "HELP"
    REPEAT_LAST = "REPEAT_LAST"  # asistanın son yanıtını yeniden söyler (yalnızca okur)
    STOP_SPEECH = "STOP_SPEECH"  # yalnızca seslendirmeyi keser; hiçbir kayda dokunmaz
    # Faz 5c.2 katılımcı düzeltmesi: üçü ayrı işlemdir ve her biri onayla uygulanır
    MARK_ABSENT = "MARK_ABSENT"  # yoklama düzeltmesi: listede kalır, "katılmadı" olur
    REMOVE_PARTICIPANT = "REMOVE_PARTICIPANT"  # yanlış kaydı bu toplantının listesinden çıkarır (silmez)
    RENAME_PARTICIPANT = "RENAME_PARTICIPANT"  # yanlış alınmış adı düzeltir / doğru kayıtla birleştirir
    LIST_PARTICIPANTS = "LIST_PARTICIPANTS"  # katılımcı listesini okur
    EXIT = "EXIT"
    UNKNOWN = "UNKNOWN"


@dataclass
class Intent:
    type: IntentType
    params: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


# Tüm kalıplar küçük harfe çevrilmiş metin üzerinde çalışır.
# Grup adları parametre adlarına karşılık gelir.
# Sayı: rakam veya sözcük ("on beş"); en uzun eşleşme önce denenir.
_NUM = "(?:\\d+|" + "|".join(sorted((re.escape(w) for w in NUMBER_WORDS), key=len, reverse=True)) + ")"
# Komut kelimesi ile parametre arasında ayraç: ":" "," "-" veya en az bir boşluk.
_SEP = r"(?:\s*[:,\-–]\s*|\s+)"

_PatternHandler = Callable[[re.Match, str], dict[str, Any]]


def _text_param(name: str) -> _PatternHandler:
    def handler(match: re.Match, original: str) -> dict[str, Any]:
        value = _original_group(match, original, name)
        return {name: value} if value else {}
    return handler


# Kayıt komutlarında komut kelimesinden sonra gelen "olarak yaz / ekle / kaydet" gibi ara sözler metne
# karışmasın ("karar olarak yaz: X" → "X"). Metin yoksa veya "sonra söyleyeceğim" gibi erteleme ise
# komut eksiktir: kayıt yapılmaz, LLM'ye de sorulmaz; kullanıcıdan metin istenir (R38 bulgusu).
_RECORD_META = re.compile(r"^(?:(?:olarak|diye)\s+)?(?:yaz|kaydet|ekle|al|gir|not et|not al|oluştur|aç)"
                          r"(?:ın|abilir misin|ar mısın)?(?=$|[\s:,\-–])[\s:,\-–]*")
_DEFERRAL = re.compile(r"^(?:ama|fakat|ancak)\b|\bsonra (?:söyle|belirt|yaz|bildir|karar ver)\w*"
                       r"|\b(?:henüz|daha) belli değil\b|\bhenüz (?:yok|söylemedim)\b"
                       r"|^(?:alındı|verildi|var|yok) m[ıiuü]\b")  # "karar alındı mı?" bir sorudur, karar değil


# Tek cümlede ikinci bir kayıt komutu (Faz 5c.1): "karar: X ve görev: Y" eskiden TEK karar olarak
# kaydediliyordu. Çoklu işlem desteklenmez; hiçbir kayıt yapılmaz ve komutları tek tek söylemek istenir.
_SECOND_COMMAND = re.compile(r"(?:^|[\s,;.])(?:karar|görev|aksiyon|not|otopark|oylama|gündeme ekle)\s*:")
# Kaydın kendisini olumsuzlayan metin: "karar: bunu henüz kaydetme" bir karar değildir
_NEGATED_RECORD = re.compile(r"^(?:(?:bunu|şunu|onu|bu kararı|bu görevi|bu notu)\s+)?(?:(?:henüz|şimdilik|daha)\s+)?"
                             r"(?:kaydetme|yazma|ekleme|not alma|girme)(?:yin|yın|yelim|yalım)?$")


def _record_param(name: str) -> _PatternHandler:
    def handler(match: re.Match, original: str) -> dict[str, Any]:
        value = _original_group(match, original, name)
        lowered = tr_lower(value)
        meta = _RECORD_META.match(lowered)
        if meta:
            value = value[meta.end():].strip(" .,:;-–")
            lowered = tr_lower(value)
        if _SECOND_COMMAND.search(lowered):
            return {name: "", "incomplete": True, "multi": True}
        if _NEGATED_RECORD.match(lowered.strip(" .!")):
            return {name: "", "incomplete": True, "negated": True}
        if not value:
            return {name: "", "incomplete": True}
        if _DEFERRAL.search(lowered):  # kullanıcı metni bilerek erteledi: diyalog soru sormaz
            return {name: "", "incomplete": True, "deferred": True}
        return {name: value}
    return handler


def _no_params(match: re.Match, original: str) -> dict[str, Any]:
    return {}


def _original_group(match: re.Match, original: str, group: str) -> str:
    """Eşleşen grubun orijinal (büyük/küçük harfi korunmuş) metnini döndürür."""
    start, end = match.span(group)
    if start < 0:
        return ""
    return original[start:end].strip(" .,:;!?\"'")


def _clean_name(name: str) -> str:
    """Son kelimedeki kesme işaretli eki atar: "Furkan'ı" → "Furkan" (ad-soyadın içi değişmez)."""
    words = name.split()
    return " ".join([*words[:-1], strip_name_suffix(words[-1])]).strip() if words else name


def _names_handler(match: re.Match, original: str) -> dict[str, Any]:
    names = [n for n in (_clean_name(n) for n in split_names(_original_group(match, original, "names"))) if n]
    return {"names": names}


def _implicit_names_handler(match: re.Match, original: str) -> dict[str, Any]:
    """"Ayşe de geldi" biçimi: "rapor geldi" gibi kişi olmayan cümleler de bu kalıba uyar. Bu yüzden listede
    olmayan küçük harfli adlar kendiliğinden eklenmez, onay sorulur (`implicit`)."""
    return {**_names_handler(match, original), "implicit": True}


def _rename_handler(match: re.Match, original: str) -> dict[str, Any]:
    name = _original_group(match, original, "name")
    new_name = _original_group(match, original, "new_name")
    return {"name": name, "new_name": new_name} if name and new_name else {}


def _speaker_handler(match: re.Match, original: str) -> dict[str, Any]:
    raw = _original_group(match, original, "speaker")
    return {"speaker": strip_name_suffix(raw).strip(), "speaker_raw": raw}


def _agenda_item_handler(match: re.Match, original: str) -> dict[str, Any]:
    title = _original_group(match, original, "title")
    minutes = None
    minutes_raw = match.groupdict().get("minutes")
    if minutes_raw:
        minutes = parse_number(minutes_raw)
    return {"title": title, "minutes": minutes}


_VOTE_KEYS = {
    "yes": r"(?:lehte|evet|kabul)",
    "no": r"(?:aleyhte|hayır|ret|red)",
    "abstain": r"(?:çekimser)",
}


def _vote_handler(match: re.Match, original: str) -> dict[str, Any]:
    body_original = _original_group(match, original, "body")
    body = tr_lower(body_original)
    counts: dict[str, int | None] = {}
    first_pos = len(body)
    for key, word in _VOTE_KEYS.items():
        # Biçim: "lehte 5 aleyhte 2 çekimser 1" (sayı, kelimeden sonra gelir)
        found = re.search(rf"\b{word}\s*:?\s*(?P<n>{_NUM})\b", body)
        value = None
        if found:
            value = parse_number(found.group("n"))
            first_pos = min(first_pos, found.start())
        counts[key] = value
    topic = body_original[:first_pos].strip(" .,:;-")
    return {"topic": topic, **counts}


@dataclass(frozen=True)
class _Rule:
    intent: IntentType
    pattern: re.Pattern
    handler: _PatternHandler = _no_params


def _rule(intent: IntentType, pattern: str, handler: _PatternHandler = _no_params) -> _Rule:
    return _Rule(intent, re.compile(pattern), handler)


def _draft_number_handler(match: re.Match, original: str) -> dict[str, Any]:
    return {"number": parse_number(match.group("number"))}


# Taslak numarası: "taslak 3", "taslak 3'ü", "3 numaralı taslağı", "üçüncü" desteklenmez (belirsiz)
_DRAFT_NO = rf"(?P<number>{_NUM})(?:'?(?:ü|u|ı|i|yı|yi|yu|yü|nolu))?"

# Sıra önemlidir: daha özel kalıplar önce gelir.
_RULES: list[_Rule] = [
    # Excel görev listesi (en başta: "görev listesini excel olarak kaydet" bir görev kaydı değildir)
    _rule(IntentType.EXPORT_TASKS,
          r"^(?:tüm |bütün )?açık görev(?:ler)?(?:i|in listesini)? excel(?:['’]?e)? (?:aktar|çıkar|kaydet|dök)$"
          r"|^(?:tüm |bütün )?açık görevleri excel (?:olarak|dosyası olarak) (?:çıkar|kaydet|aktar|ver)$",
          lambda m, o: {"scope": "open"}),
    _rule(IntentType.EXPORT_TASKS,
          r"^(?:görevleri|görev listesini|görevler) excel(?:['’]?e)? (?:aktar|çıkar|kaydet|dök)$"
          r"|^(?:görevleri|görev listesini) excel (?:olarak|dosyası olarak) (?:çıkar|kaydet|aktar|ver)$",
          lambda m, o: {"scope": "meeting"}),
    # --- Onay (yalnızca LLM'nin önerdiği bekleyen komut için; kendi başına işlem yapmaz)
    _rule(IntentType.CONFIRM, r"^(?:evet|onayla|onaylıyorum|evet onayla|tamam onayla|onay)$"),
    _rule(IntentType.REJECT_PENDING, r"^(?:hayır|vazgeç|vazgeçtim|iptal|onaylama|hayır vazgeç)$"),

    # --- Yapay zekâ taslakları ve analiz
    _rule(IntentType.ANALYZE_MEETING,
          r"^toplantıyı analiz et$|^analiz et$|^(?:karar ve görev )?adayları(?:nı)? çıkar$"
          r"|^taslak(?:ları)? (?:çıkar|oluştur)$"),
    _rule(IntentType.LIST_DRAFTS, r"^taslakları (?:oku|listele|göster|söyle)$|^taslaklar(?: neler)?$"),
    _rule(IntentType.APPROVE_DRAFT, rf"^taslak {_DRAFT_NO} onayla$", _draft_number_handler),
    _rule(IntentType.APPROVE_DRAFT, rf"^{_DRAFT_NO} numaralı taslağı onayla$", _draft_number_handler),
    _rule(IntentType.REJECT_DRAFT, rf"^taslak {_DRAFT_NO} (?:reddet|sil)$", _draft_number_handler),
    _rule(IntentType.REJECT_DRAFT, rf"^{_DRAFT_NO} numaralı taslağı (?:reddet|sil)$", _draft_number_handler),
    _rule(IntentType.READ_AI_SUMMARY, r"^özet taslağını oku$|^analiz özetini oku$|^toplantı özet taslağı$"),
    _rule(IntentType.ASK_HISTORY, rf"^soru{_SEP}(?P<question>.+)$", _text_param("question")),

    # --- Toplantı yaşam döngüsü
    _rule(IntentType.START_MEETING,
          rf"^(?:yeni )?toplantıyı (?:başlat|aç)(?:alım)?(?:{_SEP}(?P<title>.+))?$", _text_param("title")),
    _rule(IntentType.START_MEETING, r"^(?:yeni )?toplantı başlasın$|^toplantıya başla(?:yalım)?$"),
    _rule(IntentType.END_MEETING,
          r"^toplantıyı (?:bitir|kapat|sonlandır|tamamla)(?:alım)?$"
          r"|^toplantı (?:bitti|sona erdi)$|^toplantıyı bitirelim$"),

    # --- Görev tamamlama (görev: kalıbından önce gelmeli)
    _rule(IntentType.COMPLETE_ACTION,
          rf"^görev(?:i)? tamamlandı(?:{_SEP}(?P<query>.+))?$", _text_param("query")),
    _rule(IntentType.COMPLETE_ACTION,
          r"^(?P<query>.+?) görevi (?:tamamlandı|bitti|kapandı)$", _text_param("query")),
    _rule(IntentType.COMPLETE_ACTION,
          rf"^görevi (?:tamamla|kapat){_SEP}(?P<query>.+)$", _text_param("query")),
    _rule(IntentType.CANCEL_ACTION,
          rf"^görev(?:i)? iptal(?: et)?{_SEP}(?P<query>.+)$", _text_param("query")),

    # --- Son görevi düzeltme ve geri alma (yanlış anlaşılan komutlar için)
    _rule(IntentType.SET_ACTION_OWNER,
          rf"^(?:görevin )?sorumlu(?:su)?{_SEP}(?P<name>.+)$", _text_param("name")),
    _rule(IntentType.SET_ACTION_DUE,
          rf"^(?:görevin )?son tarih(?:i)?{_SEP}(?P<date_text>.+)$", _text_param("date_text")),
    _rule(IntentType.UNDO_LAST,
          r"^geri al$|^son kaydı (?:geri al|sil|iptal et)$|^sonuncuyu (?:sil|geri al)$"),

    # --- Kayıt komutları
    _rule(IntentType.ADD_DECISION, rf"^(?:karar|karar al){_SEP}(?P<text>.+)$", _record_param("text")),
    _rule(IntentType.ADD_ACTION, rf"^(?:görev|aksiyon|yeni görev){_SEP}(?P<text>.+)$", _record_param("text")),
    # STT iki nokta üst üste yazmaz: "not tedarikçi ile görüşülecek" de not komutudur.
    _rule(IntentType.ADD_NOTE, r"^(?:not|not al|not düş)\s*:\s*(?P<text>.+)$|^not(?: al| düş)?\s+(?P<text2>.+)$",
          lambda m, o: {"text": _original_group(m, o, "text") or _original_group(m, o, "text2")}),
    _rule(IntentType.ADD_PARKING,
          rf"^(?:otopark|otoparka al|otoparka ekle){_SEP}(?P<text>.+)$", _record_param("text")),
    _rule(IntentType.VOTE, rf"^(?:oylama|oyla){_SEP}(?P<body>.+)$", _vote_handler),

    # --- Gündem
    _rule(IntentType.ADD_AGENDA_ITEM,
          rf"^gündeme (?:ekle|madde ekle){_SEP}(?P<title>.+?)(?:[,\s]+(?P<minutes>{_NUM}) dakika(?:lık)?)?$",
          _agenda_item_handler),
    _rule(IntentType.READ_AGENDA,
          r"^gündemi (?:oku|söyle|listele)$|^gündem(?:de)? ne(?:ler)? var$|^gündem nedir$|^gündem$"),
    _rule(IntentType.SUMMARIZE_PREVIOUS,
          r"^(?:bir )?(?:önceki|geçen|son) toplantıyı özetle$|^(?:önceki|geçen) toplantının özeti$"
          r"|^(?:önceki|geçen) toplantı(?:da ne oldu| özeti)$"),
    # Takvim daveti (NEXT_MEETING'den ÖNCE: "sonraki toplantı için takvim daveti oluştur" bir tarih değildir)
    _rule(IntentType.CREATE_INVITE,
          r"^(?:(?:bir )?(?:sonraki|gelecek) toplantı (?:için )?)?(?:takvim|toplantı)? ?davet(?:i|ini)? "
          r"(?:oluştur|hazırla|yap|üret)$|^(?:sonraki|gelecek) toplantının (?:takvim )?davetini (?:oluştur|hazırla)$"),
    _rule(IntentType.ATTACH_INVITE,
          r"^(?:toplantı |takvim )?davet(?:i|ini) (?:e-?postaya|maile|tutanağa|tutanak e-?postasına) ekle$"),
    _rule(IntentType.NEXT_MEETING,
          rf"^(?:bir )?(?:sonraki|gelecek) toplantı(?: tarihi)?{_SEP}(?P<date_text>.+)$", _text_param("date_text")),
    _rule(IntentType.NEXT_ITEM,
          r"^(?:bir )?(?:sıradaki|sonraki|diğer) (?:madde|konu|maddeye|konuya)(?: geç(?:elim)?)?$"
          r"|^sıradaki$|^(?:diğer|sonraki) maddeye geçelim$"),
    _rule(IntentType.PREVIOUS_ITEM,
          r"^(?:bir )?önceki (?:madde|konu|maddeye|konuya)(?: dön(?:elim)?| geç)?$"),
    _rule(IntentType.SKIP_ITEM, r"^(?:bu )?(?:maddeyi|konuyu) ertele(?:yelim)?$|^ertele$"),

    # --- Görev listeleri
    _rule(IntentType.OPEN_ACTIONS,
          r"^açık görevleri (?:oku|listele|say|söyle)$|^açık görevler(?: neler)?$|^devreden görevler(?:i oku)?$"),

    # --- Konuşmacı
    _rule(IntentType.SPEAKER_STATS,
          r"^kim konuşuyor$|^konuşma süreleri$|^konuşmacı (?:istatistikleri|durumu)$|^kim ne kadar konuştu$"),
    _rule(IntentType.RELEASE_SPEAKER, r"^sözü bırak$|^söz bitti$|^konuşma bitti$|^sözü kapat$"),
    _rule(IntentType.SILENT_PARTICIPANTS,
          r"^kim(?:ler)? konuşmadı$|^konuşmayanlar(?: kim(?:ler)?)?$|^hiç konuşmayan(?:lar)?(?: var mı)?$"),
    # Onaysız kural yolu yalnızca kısa ve kesin biçim içindir: "söz Ayşe'de", "söz Mehmet Öztürk'te olsun".
    # "söz hakkını Burak Bey'e verelim" / "söz konusu rapor ..." gibi uzun cümleler buraya düşmez (HR02 bulgusu);
    # bunlar LLM önerisi + kullanıcı onayı yolunu izler.
    _rule(IntentType.SET_SPEAKER, r"^söz (?!(?:hakk|konu|ver|al)\w*\b)(?P<speaker>\S+(?: \S+){0,2})$",
          _speaker_handler),
    _rule(IntentType.SET_SPEAKER, r"^sözü (?P<speaker>.+?) ver$", _speaker_handler),
    # "ne kadar süredir konuşuyor" bir sorudur, konuşmacı adı değildir (Faz 5c.2)
    _rule(IntentType.SET_SPEAKER,
          r"^(?!(?:ne|kim|kimler|nasıl|kaç|hangi|neden|niye|kimse|hâlâ|hala)\b)"
          r"(?P<speaker>\S+(?: \S+){0,2}) konuşuyor$",
          _speaker_handler),

    # --- Katılımcı düzeltmeleri (yoklama kurallarından ÖNCE: "Ayşe yanlışlıkla katıldı" yoklama değildir)
    _rule(IntentType.LIST_PARTICIPANTS,
          r"^katılımcı listesini (?:oku|söyle|göster)$|^katılımcıları (?:oku|say|listele)$|^katılımcı listesi$"
          r"|^katılımcılar kimler$|^toplantı listesini oku$"),
    _rule(IntentType.MARK_ABSENT,
          r"^(?P<name>.+?) (?:yoklamadan|yoklama listesinden|katılanlardan) (?:çıkar|sil|düş)(?:ır mısın)?$",
          _text_param("name")),
    _rule(IntentType.MARK_ABSENT,
          r"^(?P<name>.+?) (?:yanlışlıkla|yanlış) (?:katıldı|geldi|katılmış|gelmiş|eklendi|işaretlendi)"
          r"(?: (?:olarak )?(?:görünüyor|gözüküyor|yazıldı|işaretlendi|kaydedildi))?$", _text_param("name")),
    _rule(IntentType.MARK_ABSENT,
          r"^(?P<name>.+?) (?:katılmadı|gelmedi) olarak (?:işaretle|düzelt|kaydet)$", _text_param("name")),
    _rule(IntentType.MARK_ABSENT,
          r"^(?P<name>.+?) yoklamasını (?:geri al|iptal et|kaldır|sil)$", _text_param("name")),
    _rule(IntentType.REMOVE_PARTICIPANT,
          r"^(?:yanlış(?:lıkla)? eklenen )?(?P<name>.+?)(?: kaydını| kişisini)? (?:bu )?(?:toplantıdan|"
          r"toplantı listesinden|katılımcı listesinden|katılımcılardan|listeden) (?:kaldır|çıkar|sil)$",
          _text_param("name")),
    _rule(IntentType.REMOVE_PARTICIPANT,
          r"^yanlış(?:lıkla)? eklenen (?P<name>.+?) kaydını (?:kaldır|sil|çıkar)$", _text_param("name")),
    _rule(IntentType.RENAME_PARTICIPANT,
          r"^(?P<name>.+?) olarak kaydettiğin kişinin (?:adı|ismi) (?:aslında |doğrusu |doğru olarak )?"
          r"(?P<new_name>.+?)(?: olacak| olmalı| olmalıydı)?$", _rename_handler),
    _rule(IntentType.RENAME_PARTICIPANT,
          r"^(?P<name>.+?) (?:adını|ismini) (?P<new_name>.+?) (?:olarak )?(?:düzelt|değiştir)$", _rename_handler),

    # --- Yoklama
    _rule(IntentType.ATTENDANCE,
          r"^yoklama(?: al| durumu)?$|^kim(?:ler)? (?:katıldı|burada|yok|katılmadı)$|^katılım durumu$"),
    _rule(IntentType.MARK_PRESENT_MANY, rf"^(?:katılımcılar|katılanlar){_SEP}(?P<names>.+)$", _names_handler),
    _rule(IntentType.MARK_PRESENT_MANY,
          # "aramızda" bilinçli olarak YOK: Faz 5c.1'den önce LLM + onay yolundaydı (R13); öyle kalır
          r"^(?P<names>.+?) (?:(?:toplantıya )?(?:katıldı(?:lar)?|geldi(?:ler)?)|burada(?:lar)?)$",
          _implicit_names_handler),
    _rule(IntentType.MARK_PRESENT,
          # Kesme işareti zorunlu: "Ayşenin" → Ayşe mi Ayşen mi belli değildir (o durumda LLM + onay yolu)
          r"^(?P<names>[^,']+)['’](?:n[ıiuü]n|[ıiuü]n) yoklamasını (?:al|işaretle)$", _names_handler),

    # --- Süre ve tutanak
    _rule(IntentType.TIME_STATUS,
          r"^süre durumu$|^(?:ne kadar|kaç dakika) (?:süre|zaman)? ?kaldı$|^kalan süre$|^süre ne durumda$"),
    _rule(IntentType.SEND_MINUTES,
          r"^tutanağı (?:(?P<force>tekrar|yeniden) )?(?:e-?posta(?:la|yla) )?gönder$|^tutanak gönder$",
          lambda m, o: {"force": bool(m.group("force"))}),
    _rule(IntentType.CREATE_MINUTES,
          r"^(?:ara )?tutana(?:k|ğı) (?:oluştur|hazırla|üret|yazdır)$|^ara tutanak$|^pdf oluştur$"),

    # --- Genel
    _rule(IntentType.HELP, r"^yardım$|^komutlar(?: neler)?$|^ne yapabilirsin$"),
    # Yalnızca seslendirmeyi keser (uygulamayı kapatan "çık" ile karıştırılmaz)
    _rule(IntentType.STOP_SPEECH,
          r"^(?:dur|sus|yeter|dinle|kes|sessiz ol|dur artık|tamam dur|tamam yeter|sesi kes|konuşmayı kes"
          r"|beni dinle|sus artık)$"),
    # Asistanın SON YANITI (toplantıdaki son konuşma değil). Yalnızca okur; bekleyen onayı/soruyu düşürmez.
    _rule(IntentType.REPEAT_LAST,
          r"^(?:son )?(?:söylediğini|dediğini|cevabını|yanıtını|söylediklerini) (?:bir daha |tekrar )?"
          r"(?:tekrar ?la|tekrar et|tekrar eder misin|tekrarlar mısın|söyle|söyler misin)$"
          r"|^(?:ne dedin|ne demiştin|tekrar eder misin|bir daha söyler misin|son yanıtı tekrarla)$"),
    _rule(IntentType.EXIT, r"^çık$|^çıkış$|^programı kapat$|^programdan çık$"),
]

_LEADING_FILLERS = ("hey ", "tamam ", "selam ")

NEGATED_MESSAGE = "Anlaşıldı, kaydetmiyorum; hiçbir işlem yapılmadı."
MULTI_COMMAND_MESSAGE = ("Bu cümlede birden fazla işlem var; güvenlik için hiçbiri yapılmadı. Lütfen her birini ayrı "
                         "söyleyin (ör. önce «Jarvis, karar: ...», sonra «Jarvis, görev: ...»).")

_QUESTION_FORM = re.compile(r"\?|\b(?:mı|mi|mu|mü|ne zaman|nasıl|neden|kim)\b")
_RECORD_INTENTS = frozenset({IntentType.ADD_DECISION, IntentType.ADD_ACTION, IntentType.ADD_NOTE,
                             IntentType.ADD_PARKING})

# Gerçek mikrofon testinde (Faz 5a.2) görülen Google tr-TR bozulmaları. Yalnızca komut konumunda ve
# dar kalıplarla uygulanır; genel yazım düzeltmesi yapılmaz. Sonuç her zaman kullanıcı onayına sunulur.
_STT_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:carrer|carer|karer|kerar|carar|karrar|kar ar)(?=$|[\s:,])", re.IGNORECASE), "karar"),
    (re.compile(r"\bmaddeye ertele", re.IGNORECASE), "maddeyi ertele"),
    (re.compile(r"(?<=\s)(?:alekte|aleyte|alehte|aleyde)(?=\s)", re.IGNORECASE), "aleyhte"),
]
_RECORD_LABELS = {IntentType.ADD_DECISION: "karar metni", IntentType.ADD_ACTION: "görev metni",
                  IntentType.ADD_PARKING: "otoparka alınacak konu"}
_RECORD_EXAMPLES = {IntentType.ADD_DECISION: "karar: yeni plan pazartesi uygulanacak",
                    IntentType.ADD_ACTION: "görev: Mehmet raporu cumaya kadar hazırlasın",
                    IntentType.ADD_PARKING: "otopark: ofis taşınması"}


class CommandParser:
    """Uyandırma kelimesini algılar ve komutu niyete dönüştürür."""

    def __init__(self, wake_word: str = "jarvis", aliases: list[str] | None = None,
                 command_prefix: str | None = "!", near_misses: list[str] | None = None):
        words = [wake_word, *(aliases or [])]
        self._wake_words = sorted({ascii_fold(w).strip(" '") for w in words if w.strip()}, key=len, reverse=True)
        self._command_prefix = command_prefix
        # Hitap sayılmayan ama "Jarvis" olabilecek yakın tanımalar (ör. "charles"). Varsayılan BOŞ; .env ile
        # açılır. Bu kelimeyle başlayan cümle asla doğrudan komut olmaz; yalnızca onay önerisi üretir.
        near = {ascii_fold(w).strip(" '") for w in (near_misses or []) if w.strip()}
        self._near_misses = near - set(self._wake_words)

    def split_near_miss(self, text: str) -> tuple[str, str] | None:
        """Metin bir yakın hitapla başlıyorsa (kelime, kalan) döndürür; aksi halde None."""
        if not self._near_misses:
            return None
        first, _, rest = (text or "").strip().partition(" ")
        word = first.strip(" ,.:;!?")
        if "'" in word or "’" in word:  # "Charles'ın" gibi ekli kullanım hitap değildir
            return None
        if ascii_fold(word) in self._near_misses and rest.strip():
            return word, rest.strip(" ,.:;!")
        return None

    # ---------------------------------------------------------------- uyandırma
    def split_wake_word(self, text: str) -> tuple[bool, str]:
        """Metin uyandırma kelimesiyle başlıyorsa (True, komut) döndürür."""
        stripped = text.strip()
        if self._command_prefix and stripped.startswith(self._command_prefix):
            return True, stripped[len(self._command_prefix):].strip()
        working = stripped
        lowered = tr_lower(working)
        for filler in _LEADING_FILLERS:
            if lowered.startswith(filler):
                working = working[len(filler):]
                break
        first, _, rest = working.partition(" ")
        # Ekli biçimler ("Jarvis'in raporu") hitap değildir; yalnızca yalın kelime kabul edilir.
        first_clean = first.strip(" ,.:;!?")
        if ascii_fold(first_clean) in self._wake_words:
            return True, rest.strip(" ,.:;!")
        # "Jarvis," gibi bitişik noktalama: "jarvis,toplantıyı başlat"
        match = re.match(r"^\s*([\w']+)[,.:;!]\s*(.*)$", working)
        if match and ascii_fold(match.group(1)).strip("'") in self._wake_words:
            return True, match.group(2).strip()
        return False, stripped

    def is_addressed(self, text: str) -> bool:
        return self.split_wake_word(text)[0]

    def split_embedded_command(self, text: str) -> tuple[str, str] | None:
        """Cümle ortasındaki hitabı ayırır: "böyle karar verdik Jarvis sıradaki madde"
        -> ("böyle karar verdik", "Jarvis sıradaki madde").

        Ses tanıma bazen konuşmayla komutu tek parça döndürür. Yalnızca yalın kelime
        olarak geçen uyandırma kelimesi dikkate alınır ("Jarvis'in" sayılmaz).
        """
        words = text.split()
        for index in range(1, len(words)):
            token = words[index].strip(" ,.:;!?\"")
            if token and ascii_fold(token) in self._wake_words:
                before = " ".join(words[:index]).strip(" ,.;:")
                return before, " ".join(words[index:])
        return None

    # ---------------------------------------------------------------- ayrıştırma
    def parse(self, text: str, require_wake: bool = True) -> Intent | None:
        """Komutu ayrıştırır.

        Dönüş: Jarvis'e hitap edilmemişse None; edilmiş ama anlaşılamamışsa
        UNKNOWN; aksi halde ilgili Intent.
        """
        addressed, command = self.split_wake_word(text)
        if not addressed:
            if require_wake:
                return None
            command = text
        return self.parse_command(command, raw_text=text)

    def parse_command(self, command: str, raw_text: str | None = None) -> Intent:
        """Uyandırma kelimesi ayrılmış komut metnini ayrıştırır."""
        original = normalize_spaces(command).strip(" .!?")
        raw = raw_text if raw_text is not None else command
        if not original:
            return Intent(IntentType.UNKNOWN, {"reason": "empty"}, raw)
        lowered = tr_lower(original)
        # tr_lower uzunluğu korur; korunmazsa (nadir Unicode) küçük harfli metin kullanılır.
        source = original if len(lowered) == len(original) else lowered
        for rule in _RULES:
            match = rule.pattern.match(lowered)
            if match:
                params = rule.handler(match, source)
                if params.pop("negated", False):
                    return Intent(IntentType.UNKNOWN, {"command": original, "clarify": NEGATED_MESSAGE}, raw)
                if params.pop("multi", False):
                    return Intent(IntentType.UNKNOWN, {"command": original, "clarify": MULTI_COMMAND_MESSAGE}, raw)
                if params.pop("incomplete", False):
                    label = _RECORD_LABELS.get(rule.intent, "metin")
                    unknown = {"command": original, "clarify": (
                        f"{label.capitalize()} duyulmadı; hiçbir kayıt yapılmadı. Lütfen «Jarvis, "
                        f"{_RECORD_EXAMPLES.get(rule.intent, 'komut: metin')}» biçiminde tekrar söyleyin.")}
                    if not params.pop("deferred", False):
                        # diyalog yöneticisi eksik metni kullanıcıya sorabilir (Faz 5c.2)
                        unknown["incomplete_intent"] = rule.intent.value
                    return Intent(IntentType.UNKNOWN, unknown, raw)
                if rule.intent == IntentType.MARK_PRESENT_MANY and len(params.get("names", [])) == 1:
                    return Intent(IntentType.MARK_PRESENT, params, raw)
                if rule.intent == IntentType.VOTE and (params.get("yes") is None or params.get("no") is None):
                    suggestion = self._stt_suggestion(original, raw)
                    if suggestion is not None:
                        return suggestion
                return Intent(rule.intent, params, raw)
        suggestion = self._stt_suggestion(original, raw)
        if suggestion is not None:
            return suggestion
        return Intent(IntentType.UNKNOWN, {"command": original}, raw)

    def _stt_suggestion(self, original: str, raw: str) -> Intent | None:
        """Gerçek mikrofon testinde görülen, açık ve dar STT bozulmalarını dener (Faz 5a.3).

        Sonuç ÇALIŞTIRILMAZ: UNKNOWN döner ve düzeltilmiş komut `stt_suggestion` olarak taşınır; asistan
        kullanıcıya "Şunu mu demek istediniz?" diye sorar ve "Jarvis, evet" bekler.
        """
        fixed = original
        applied = []
        for pattern, replacement in _STT_FIXES:
            new = pattern.sub(replacement, fixed, count=1)
            if new != fixed:
                applied.append(f"{pattern.search(fixed).group(0)} → {replacement}")
                fixed = new
        if not applied:
            return None
        intent = self.parse_command(fixed)
        if intent.type == IntentType.UNKNOWN or intent.get("stt_suggestion"):
            return None
        if intent.type == IntentType.VOTE and (intent.get("yes") is None or intent.get("no") is None):
            return None  # oy sayısı belirsiz: tahmin yürütülmez
        if intent.type in _RECORD_INTENTS and _QUESTION_FORM.search(tr_lower(raw or original)):
            return None  # soru cümlesi kayıt önerisine dönüşmez ("carrer görüşmesi ne zaman?")
        return Intent(IntentType.UNKNOWN, {"command": original, "stt_suggestion": fixed, "stt_fixes": applied,
                                           "suggested_intent": intent.type.value}, raw)


HELP_TEXT = """Kullanılabilir komutlar (başına "Jarvis" ekleyin):
  toplantıyı başlat [başlık]      toplantıyı bitir
  yoklama al                      Ahmet katıldı / katılımcılar: Ahmet, Ayşe
  gündemi oku                     gündeme ekle: Bütçe, 15 dakika
  sıradaki madde / önceki madde   maddeyi ertele
  önceki toplantıyı özetle        açık görevleri oku
  söz Ayşe'de / Ayşe konuşuyor    sözü bırak / kim konuşmadı
  karar: ...                      görev: Mehmet raporu haftaya kadar hazırlasın
  görev tamamlandı: rapor         görev iptal: rapor
  sorumlu: Ayşe (son görev için)  son tarih: 21 ekim (son görev için)
  geri al (son kaydı siler)       kim konuşuyor / konuşma süreleri
  not: ...
  otopark: ...                    oylama: konu lehte 5 aleyhte 2 çekimser 1
  süre durumu                     sonraki toplantı 21 ekim saat 14.00 (takvim daveti oluşur)
  takvim daveti oluştur           toplantı davetini e-postaya ekle
  görevleri Excel'e aktar         açık görevleri Excel olarak çıkar
  tutanak oluştur (PDF)           tutanağı gönder / tutanağı tekrar gönder
  toplantıyı analiz et (yerel LLM) taslakları oku / taslak 3 onayla / taslak 3 reddet
  özet taslağını oku              soru: geçen toplantıda bütçe için ne karar verdik?
  evet / hayır (Jarvis'in önerdiği komutu onaylar / reddeder)
  son söylediğini tekrar et (Jarvis'in son yanıtı)
  dur / sus / yeter / dinle (yalnızca sesli yanıtı keser; klavyede Esc)
  katılımcı listesini oku         Ahmet'i yoklamadan çıkar (katılmadı olarak işaretler)
  Recep'i bu toplantıdan kaldır   Ahmet olarak kaydettiğin kişinin adı aslında Ahmet Yılmaz
  yardım / çık"""
