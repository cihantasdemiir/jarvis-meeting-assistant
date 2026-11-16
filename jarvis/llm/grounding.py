"""Kanıta dayalılık (grounding) denetimleri.

LLM'nin döndürdüğü isim, metin ve tarih ifadelerinin kullanıcının gerçekten söylediği
metinde (veya kanıt transkript satırlarında) geçip geçmediğini deterministik olarak denetler.
Böylece model bilgi uyduramaz: söylenmemiş bir isim/tarih kabul edilmez.
"""
from __future__ import annotations

import re

from ..utils.turkish_text import ascii_fold

_STOPWORDS = {
    "bir", "bu", "şu", "ve", "ile", "için", "ama", "fakat", "gibi", "kadar", "olarak", "olan", "daha", "çok",
    "her", "ise", "de", "da", "ki", "mi", "mı", "mu", "mü", "ne", "en", "o", "biz", "siz", "ben", "sen",
    "olsun", "olacak", "edilecek", "yapılacak", "lütfen", "jarvis",
}
_WORD = re.compile(r"[0-9A-Za-zÇĞİÖŞÜçğıöşüâîû]+")


def content_words(text: str) -> list[str]:
    words = [ascii_fold(w) for w in _WORD.findall(text or "")]
    folded_stop = {ascii_fold(s) for s in _STOPWORDS}
    return [w for w in words if len(w) >= 3 and w not in folded_stop]


def _word_matches(word: str, source_word: str) -> bool:
    """Türkçe ekleri tolere eder: "rapor" ~ "raporu", "hazırla" ~ "hazırlasın"."""
    if word == source_word:
        return True
    common = 0
    for a, b in zip(word, source_word, strict=False):
        if a != b:
            break
        common += 1
    shorter = min(len(word), len(source_word))
    return common >= 4 or (shorter >= 3 and common == shorter)


def grounded_ratio(value: str, source: str) -> float:
    """`value` içindeki içerik kelimelerinin `source` içinde geçme oranı (0..1)."""
    words = content_words(value)
    if not words:
        return 0.0
    source_words = content_words(source) + [ascii_fold(w) for w in _WORD.findall(source or "")]
    hits = sum(any(_word_matches(w, s) for s in source_words) for w in words)
    return hits / len(words)


def is_grounded(value: str, source: str, threshold: float = 0.8) -> bool:
    return bool(value and value.strip()) and grounded_ratio(value, source) >= threshold


def phrase_in(value: str, source: str) -> bool:
    """Kısa ifade (tarih, isim) kaynakta neredeyse aynen geçiyor mu? Her kelime eşleşmeli."""
    words = [ascii_fold(w) for w in _WORD.findall(value or "")]
    if not words:
        return False
    source_words = [ascii_fold(w) for w in _WORD.findall(source or "")]
    return all(any(_word_matches(w, s) or w == s for s in source_words) for w in words)
