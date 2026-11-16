"""Görev metninden sorumlu, son tarih ve açıklama çıkarımı.

Örnek: "Mehmet raporu haftaya kadar hazırlasın" (toplantı 14.10.2026)
    -> sorumlu: Mehmet Öztürk, son tarih: 21.10.2026, açıklama: "raporu hazırlasın"

İlkeler (talimat Bölüm 8):
  * Sorumlu yalnızca katılımcı listesindeki bir kişiyle *tek* eşleşme varsa atanır.
    Birden fazla aday varsa veya kimse bulunmazsa sorumlu boş bırakılır ve teyit istenir.
  * Tarih yalnızca tanınan bir ifadeden çıkarılır; tahmin yapılmaz.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..utils.dates import DateMatch, find_date, remove_span
from ..utils.turkish_text import (
    NameMatch,
    ascii_fold,
    best_name_match,
    candidate_name_forms,
    normalize_spaces,
    strip_name_suffix,
)

# "Sorumlu: Ayşe" veya "... sorumlu Ayşe" gibi açık sorumlu belirtimi
_EXPLICIT_OWNER = re.compile(r"(?:^|[\s,;])(?:sorumlu(?:su)?|sorumlu kişi)\s*:?\s*(?P<name>[^,;]+?)\s*(?:[,;]|$)",
                             re.IGNORECASE)


@dataclass
class ActionDraft:
    description: str
    owner: str = ""
    owner_spoken: str = ""
    owner_candidates: list[str] = field(default_factory=list)
    due_date: date | None = None
    date_text: str = ""
    date_note: str = ""

    @property
    def owner_ambiguous(self) -> bool:
        return not self.owner and len(self.owner_candidates) > 1

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.owner:
            missing.append("sorumlu")
        if self.due_date is None:
            missing.append("son tarih")
        return missing


def _strict_match(word_or_phrase: str, names: list[str]) -> NameMatch:
    """Benzerlik (yazım hatası) toleransı olmadan eşleştirme: yalnızca ad/soyad kelimeleri."""
    result = best_name_match(word_or_phrase, names, fuzzy_threshold=1.01)
    return result


def _find_owner(words: list[str], names: list[str]) -> tuple[NameMatch | None, int, int]:
    """Metnin başındaki 1-3 kelimeyi isim olarak dener.

    Dönüş: (eşleşme, başlangıç kelime indeksi, bitiş kelime indeksi).
    """
    for length in (3, 2, 1):
        if len(words) < length:
            continue
        phrase = " ".join(strip_name_suffix(w) for w in words[:length])
        match = _strict_match(phrase, names)
        if match.found or match.is_ambiguous:
            # İki kelimelik eşleşme yalnızca ikinci kelime de isme aitse kabul edilir
            if length > 1 and match.found:
                name_words = ascii_fold(match.name or "").split()
                if not all(ascii_fold(strip_name_suffix(w)) in name_words for w in words[:length]):
                    continue
            return match, 0, length
    return None, 0, 0


def _find_owner_anywhere(words: list[str], names: list[str]) -> tuple[NameMatch | None, int]:
    """Başta isim yoksa metnin içinde ekli/eksiz isim arar ("raporu Mehmet hazırlasın")."""
    found: list[tuple[NameMatch, int]] = []
    for index, word in enumerate(words):
        if not word[:1].isupper() and "'" not in word and "’" not in word:
            continue  # STT çoğunlukla isimleri büyük harfle yazar; küçük kelimeler isim sayılmaz
        for form in candidate_name_forms(word):
            match = _strict_match(form, names)
            if match.found or match.is_ambiguous:
                found.append((match, index))
                break
    unique_names = {m.name for m, _ in found if m.found}
    if len(found) == 1 or (len(unique_names) == 1 and all(m.found for m, _ in found)):
        return found[0]
    if found:
        candidates = sorted({c for m, _ in found for c in (m.candidates or [m.name]) if c})
        return NameMatch(query=" ".join(words[i] for _, i in found), candidates=candidates), -1
    return None, -1


def extract_action(text: str, participant_names: list[str], base: date) -> ActionDraft:
    """Görev cümlesini ayrıştırır."""
    text = normalize_spaces(text).strip(" .")
    draft = ActionDraft(description=text)

    # 1) Son tarih
    working = text
    date_match: DateMatch | None = find_date(working, base)
    if date_match:
        draft.due_date = date_match.value
        draft.date_text = date_match.text
        draft.date_note = date_match.note
        working = remove_span(working, date_match.start, date_match.end)

    # 2) Açık "sorumlu:" belirtimi
    explicit = _EXPLICIT_OWNER.search(working)
    owner_match: NameMatch | None = None
    if explicit:
        spoken = explicit.group("name").strip()
        owner_match = best_name_match(spoken, participant_names)
        draft.owner_spoken = spoken
        working = remove_span(working, explicit.start(), explicit.end())
    else:
        words = working.split()
        owner_match, start, end = _find_owner(words, participant_names)
        if owner_match is not None:
            draft.owner_spoken = " ".join(words[start:end])
            words = words[:start] + words[end:]
        else:
            owner_match, index = _find_owner_anywhere(words, participant_names)
            if owner_match is not None and index >= 0:
                draft.owner_spoken = words[index]
                words = words[:index] + words[index + 1:]
            elif owner_match is not None:
                draft.owner_spoken = owner_match.query
        working = " ".join(words)

    if owner_match is not None:
        if owner_match.found:
            draft.owner = owner_match.name or ""
        else:
            draft.owner_candidates = list(owner_match.candidates)

    draft.description = normalize_spaces(working).strip(" ,;:.") or text
    return draft
