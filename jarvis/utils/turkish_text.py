"""Türkçe metin yardımcıları.

Python'un `str.lower()` fonksiyonu Türkçe 'I' ve 'İ' harflerini yanlış
dönüştürür ("I".lower() == "i", "İ".lower() == "i̇"). Bu modüldeki fonksiyonlar
Türkçe kurallara göre çalışır.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})
_UPPER_MAP = str.maketrans({"i": "İ", "ı": "I"})

# Türkçe karakterleri ASCII karşılığına indirger (yalnızca eşleştirme için).
_ASCII_FOLD = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
    "â": "a", "î": "i", "û": "u",
})

_APOSTROPHES = "'’‘`´"

# İsimlere eklenen yaygın durum ekleri ("Ayşe'de", "Mehmet'e", "Ali'nin").
_NAME_SUFFIXES = sorted({
    "da", "de", "ta", "te", "dan", "den", "tan", "ten",
    "a", "e", "ya", "ye", "ı", "i", "u", "ü", "yı", "yi", "yu", "yü",
    "ın", "in", "un", "ün", "nın", "nin", "nun", "nün",
    "la", "le", "yla", "yle", "ca", "ce",
}, key=len, reverse=True)

NUMBER_WORDS: dict[str, int] = {
    "sıfır": 0, "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6,
    "yedi": 7, "sekiz": 8, "dokuz": 9, "on": 10, "on bir": 11, "on iki": 12,
    "on beş": 15, "yirmi": 20, "yirmi beş": 25, "otuz": 30, "kırk": 40,
    "kırk beş": 45, "elli": 50, "altmış": 60, "doksan": 90, "yarım": 0,
}


def tr_lower(text: str) -> str:
    """Türkçe kurallarına göre küçük harfe çevirir."""
    return text.translate(_LOWER_MAP).lower()


def tr_upper(text: str) -> str:
    """Türkçe kurallarına göre büyük harfe çevirir."""
    return text.translate(_UPPER_MAP).upper()


def tr_title(text: str) -> str:
    """Her kelimenin ilk harfini Türkçe kurallarıyla büyütür."""
    words = []
    for word in text.split():
        words.append(tr_upper(word[:1]) + tr_lower(word[1:]))
    return " ".join(words)


def ascii_fold(text: str) -> str:
    """Eşleştirme için Türkçe karakterleri sadeleştirir ("Öztürk" -> "ozturk")."""
    lowered = tr_lower(text).translate(_ASCII_FOLD)
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_for_matching(text: str) -> str:
    """Noktalama işaretlerini atar, küçük harfe çevirir, boşlukları sadeleştirir.

    Kesme işareti korunur; isim eklerinin ayrılmasında kullanılır.
    """
    lowered = tr_lower(text)
    lowered = re.sub(rf"[^\w\s{_APOSTROPHES}:]", " ", lowered)
    return normalize_spaces(lowered)


def strip_name_suffix(word: str) -> str:
    """İsimden kesme işaretli eki ayırır: "Ayşe'de" -> "Ayşe".

    Kesme işareti yoksa kelime olduğu gibi döndürülür; çünkü ekli/eksiz ayrımı
    kesme işareti olmadan güvenilir yapılamaz (eşleştirme bunu ayrıca dener).
    """
    for mark in _APOSTROPHES:
        if mark in word:
            return word.split(mark, 1)[0]
    return word


def candidate_name_forms(word: str) -> list[str]:
    """Kesme işareti olmayan ekli isimler için olası kök biçimlerini üretir.

    STT çoğu zaman kesme işareti yazmaz ("söz aysede"). Bu yüzden ekli
    biçimden olası kökleri çıkarırız; doğru kök ancak katılımcı listesindeki
    bir isimle eşleşirse kabul edilir.
    """
    base = strip_name_suffix(word)
    forms = [base]
    low = tr_lower(base)
    for suffix in _NAME_SUFFIXES:
        if low.endswith(suffix) and len(low) - len(suffix) >= 2:
            forms.append(base[: len(base) - len(suffix)])
    # "Mehmet'e" -> "Mehmete": son ünsüz yumuşaması gibi durumlar burada ele alınmaz.
    seen: set[str] = set()
    unique = []
    for form in forms:
        key = tr_lower(form)
        if key not in seen:
            seen.add(key)
            unique.append(form)
    return unique


def parse_number(text: str) -> int | None:
    """"15", "on beş", "yirmi" gibi ifadeleri sayıya çevirir."""
    cleaned = normalize_spaces(tr_lower(text))
    if cleaned.isdigit():
        return int(cleaned)
    if cleaned in NUMBER_WORDS:
        return NUMBER_WORDS[cleaned]
    parts = cleaned.split()
    if len(parts) == 2 and parts[0] in NUMBER_WORDS and parts[1] in NUMBER_WORDS:
        tens, ones = NUMBER_WORDS[parts[0]], NUMBER_WORDS[parts[1]]
        if tens % 10 == 0 and tens >= 10 and 0 < ones < 10:
            return tens + ones
    return None


# İsim listesindeki bağlaçlar. Yalnızca BAĞIMSIZ kelime olarak geçtiklerinde ayraçtır; "Veli", "Vildan",
# "İlker" gibi isimlerin içi hiçbir zaman değiştirilmez. "v"/"V": Google STT bazen "ve" yerine tek harf yazar
# (Faz 5c.1 bulgusu: "Ayşe Mehmet V Furkan"). Noktalı "V." bir baş harf olabileceği için ayraç sayılmaz.
_NAME_CONJUNCTIONS = re.compile(r"\s+(?:ve|ile|bir de|ayrıca|v|&)\s+|\s*[;/]\s*", re.IGNORECASE)
# "Ayşe de geldi", "Mehmet'le Ayşe de" → isimden sonraki "de/da" ilgeci isme dahil değildir
_TRAILING_CLITIC = re.compile(r"\s+(?:de|da)$", re.IGNORECASE)
_LEADING_CONJ = re.compile(r"^(?:ve|ile|bir de|ayrıca)\s+", re.IGNORECASE)


def split_names(text: str) -> list[str]:
    """"Kenan, Ayşe ve Mehmet" -> ["Kenan", "Ayşe", "Mehmet"]. "Ayşe Mehmet v Furkan" -> ["Ayşe Mehmet", "Furkan"].

    Virgülsüz bitişik adlar ("Ayşe Mehmet") burada BÖLÜNMEZ: ad-soyad mı, iki kişi mi olduğu ancak katılımcı
    listesiyle anlaşılabilir (bkz. MeetingManager._expand_name_chunks); bilinmiyorsa kullanıcıya sorulur.
    """
    unified = _NAME_CONJUNCTIONS.sub(",", f" {text} ").strip()
    names = []
    for part in unified.split(","):
        name = normalize_spaces(part).strip(" .")
        name = _LEADING_CONJ.sub("", _TRAILING_CLITIC.sub("", name)).strip(" .")
        if name:
            names.append(name)
    return names


@dataclass
class NameMatch:
    """İsim eşleştirme sonucu."""

    query: str
    name: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return self.name is None and len(self.candidates) > 1

    @property
    def found(self) -> bool:
        return self.name is not None


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher  # noqa: PLC0415

    return SequenceMatcher(None, a, b).ratio()


# Hitap/unvan sözcükleri: isim eşleştirmede yok sayılır ("Ayşe Hanım'a" → "Ayşe"). Yalnızca bu tam biçimler;
# "Beyza" gibi adlar etkilenmez.
_HONORIFICS = {
    "bey", "beyi", "beye", "beyin", "beyde", "beyden", "beyle", "bayan", "bay",
    "hanim", "hanimi", "hanima", "hanimin", "hanimda", "hanimdan", "hanimla",
    "hoca", "hocam", "hocamiz", "hocaya", "sayin", "dr", "doktor", "mudur", "muduru",
}


def strip_honorifics(query: str) -> str:
    """Unvanları çıkarır; geriye kelime kalmazsa metni olduğu gibi döndürür."""
    words = query.split()
    kept = [w for w in words if ascii_fold(strip_name_suffix(w)).strip(" .,") not in _HONORIFICS]
    return " ".join(kept) if kept else query


def best_name_match(query: str, names: list[str], fuzzy_threshold: float = 0.8) -> NameMatch:
    """Söylenen ismi katılımcı listesindeki tam adla eşleştirir.

    Kurallar (yanlış kişiye görev atamamak için temkinli):
      1. Tam ad birebir eşleşirse onu seç.
      2. Söylenen kelimeler bir katılımcının ad/soyad kelimeleriyle örtüşüyorsa
         ve tek aday varsa onu seç; birden fazla aday varsa belirsiz say.
      3. Ek alınmış biçimler ("Aysede") için olası kökler denenir.
      4. Son çare olarak yüksek eşikli benzerlik (STT yazım hataları için);
         yine tek aday şartı aranır.
    """
    query = normalize_spaces(query)
    result = NameMatch(query=query)
    if not query or not names:
        return result
    query = strip_honorifics(query)

    folded_names = {name: ascii_fold(name) for name in names}
    query_words = [ascii_fold(strip_name_suffix(w)) for w in query.split()]
    query_folded = " ".join(query_words)

    # 1) Tam eşleşme
    exact = [n for n, f in folded_names.items() if f == query_folded]
    if len(exact) == 1:
        result.name = exact[0]
        return result

    # 2) Kelime örtüşmesi (ad, soyad veya ikisi birden)
    def word_subset_matches(words: list[str]) -> list[str]:
        hits = []
        for name, folded in folded_names.items():
            name_words = folded.split()
            if words and all(w in name_words for w in words):
                hits.append(name)
        return hits

    hits = word_subset_matches(query_words)
    if not hits and len(query.split()) == 1:
        # 3) Kesme işaretsiz ekli biçimler
        for form in candidate_name_forms(query):
            hits = word_subset_matches([ascii_fold(form)])
            if hits:
                break

    if len(hits) == 1:
        result.name = hits[0]
        return result
    if len(hits) > 1:
        result.candidates = hits
        return result

    # 4) Benzerlik (yazım hatası toleransı)
    scored: list[tuple[float, str]] = []
    for name, folded in folded_names.items():
        best = _similarity(query_folded, folded)
        for word in folded.split():
            best = max(best, _similarity(query_folded, word))
        if best >= fuzzy_threshold:
            scored.append((best, name))
    scored.sort(reverse=True)
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.08):
        result.name = scored[0][1]
    elif len(scored) > 1:
        result.candidates = [name for _, name in scored]
    return result


_BACK_VOWELS = "aıou"
_FRONT_VOWELS = "eiöü"
_HARD_CONSONANTS = "fstkçşhp"


def locative_suffix(name: str) -> str:
    """İsme bulunma eki ekler: "Ayşe" -> "Ayşe'de", "Yılmaz" -> "Yılmaz'da", "Öztürk" -> "Öztürk'te"."""
    word = name.strip()
    if not word:
        return word
    lowered = tr_lower(word)
    vowel = next((ch for ch in reversed(lowered) if ch in _BACK_VOWELS + _FRONT_VOWELS), "e")
    harmony = "a" if vowel in _BACK_VOWELS else "e"
    consonant = "t" if lowered[-1] in _HARD_CONSONANTS else "d"
    return f"{word}'{consonant}{harmony}"


def dative_suffix(name: str) -> str:
    """İsme yönelme eki ekler: "Selin Uçar" -> "Selin Uçar'a", "Ayşe" -> "Ayşe'ye", "Mehmet" -> "Mehmet'e"."""
    word = name.strip()
    if not word:
        return word
    lowered = tr_lower(word)
    vowel = next((ch for ch in reversed(lowered) if ch in _BACK_VOWELS + _FRONT_VOWELS), "e")
    harmony = "a" if vowel in _BACK_VOWELS else "e"
    buffer = "y" if lowered[-1] in _BACK_VOWELS + _FRONT_VOWELS else ""
    return f"{word}'{buffer}{harmony}"


def expand_name_chunks(names: list[str], participants: list[str]) -> list[str]:
    """Virgülsüz STT çıktısını ayırır: "Kenan Ayşe" -> ["Kenan", "Ayşe"].

    Yalnızca parça bir bütün olarak eşleşmiyorsa ve her kelime ayrı ayrı tek bir katılımcıyla eşleşiyorsa
    bölünür; aksi halde olduğu gibi kalır (ad-soyad mı iki kişi mi bilinemez).
    """
    expanded: list[str] = []
    for chunk in names:
        words = chunk.split()
        match = best_name_match(chunk, participants)
        if len(words) > 1 and not match.found and not match.is_ambiguous:
            if all(best_name_match(word, participants).found for word in words):
                expanded.extend(words)
                continue
        expanded.append(chunk)
    return expanded


# "Sorumlu yok", "belirsiz", "bilmiyorum" gibi yanıtlar bir değer değil, değerin OLMADIĞINI söyler (Faz 5c.2 canlı
# test: "sorumlu yok son tarih yok" kişi adı olarak "Yok Son Tarih Yok" kaydedilmişti).
_NONE_VALUE = re.compile(
    r"^(?:(?:sorumlu(?:su)?|son tarih(?:i)?|tarih(?:i)?)\s+)?"
    r"(?:yok|yoktur|belirsiz|bilmiyorum|bilinmiyor|kimse|hiç kimse|henüz yok|belli değil|şimdilik yok|"
    r"belirtilmedi|boş|boş kalsın|gerek yok)$")
# Kişi adı içinde geçemeyecek sözcükler: alan adları, olumsuzluk ve dolgu sözcükleri
_NOT_NAME_WORDS = frozenset({"yok", "var", "son", "tarih", "tarihi", "sorumlu", "sorumlusu", "belirsiz", "bilmiyorum",
                             "kimse", "hiç", "henüz", "değil", "evet", "hayır", "görev", "karar", "gün", "kadar"})


def is_none_value(text: str) -> bool:
    """Metin bir değer değil, "değer yok" yanıtı mı? ("yok", "sorumlu yok", "belirsiz"...)"""
    cleaned = " ".join(tr_lower(text or "").replace("’", "'").strip(" .,!?;:").split())
    return bool(_NONE_VALUE.match(cleaned))


def plausible_person_name(text: str) -> bool:
    """Liste dışı bir sorumlu adı olarak kabul edilebilir mi? Alan adı, "yok" veya rakam içeren metin ad değildir."""
    words = [w.strip(" .,;:!?") for w in (text or "").split()]
    words = [w for w in words if w]
    if not words or len(words) > 4:
        return False
    return all(w.replace("'", "").replace("’", "").isalpha() and tr_lower(strip_name_suffix(w)) not in _NOT_NAME_WORDS
               for w in words)
