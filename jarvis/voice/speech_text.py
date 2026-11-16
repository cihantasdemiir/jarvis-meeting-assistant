"""Seslendirme için metin hazırlığı (Faz 5b).

Bu modül yalnızca hoparlöre gidecek KOPYAYI üretir. Konsolda gösterilen, transkripte veya veritabanına
yazılan özgün metin hiçbir zaman değiştirilmez (`to_speech_text` saf bir fonksiyondur).

Sayıların, tarihlerin (15.10.2026), saatlerin (14:30), yüzdelerin ve yaygın kısaltmaların okunuşu
Pocket TTS Turkish paketinin kendi normalleştiricisi tarafından yapılır. Burada yalnızca o normalleştiricinin
J.A.R.V.I.S yanıtlarında yanlış okuduğu kalıplar ele alınır (geliştirme ortamında paketin normalleştiricisi
J.A.R.V.I.S yanıtlarıyla denenerek belirlendi):

  * ISO tarih (2026-10-22) → 22.10.2026 (paket "iki bin yirmi altı-on-yirmi iki" okuyordu)
  * Dosya yolu (output/demo/...pdf) → "ekrandaki dosya" (paket rakamları tek tek okuyordu)
  * E-posta adresi → "e-posta adresi"
  * J.A.R.V.I.S → Jarvis; PDF, SMTP, LLM gibi kısaltmalar harf harf
  * Satır sonları ve çok boşluklu sütunlar (yardım metni) → cümle/virgül
  * Ok işaretleri → virgül
  * Çok uzun yanıt → baştaki tam cümleler + "Ayrıntılar ekranda."
"""
from __future__ import annotations

import re

_JARVIS = re.compile(r"\bJ\.A\.R\.V\.I\.S\.?", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b((?:19|20)\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_EXTENSIONS = "pdf|eml|ics|wav|json|csv|md|db|xlsx|log|txt|ps1|py|yaml"
# Dosya yolu: uzantılı ad (klasörlü veya klasörsüz) ya da en az iki ayraçlı klasör yolu.
# "12/15 dk" veya "2/4" gibi kesirler yol sayılmaz.
_PATH = re.compile(
    rf"(?:[A-Za-z]:)?[\\/]?(?:[\w.\-]+[\\/])*[\w\-][\w.\-]*\.(?:{_EXTENSIONS})\b"
    r"|(?:[A-Za-z]:)?[\\/]?(?:[\w.\-]+[\\/]){2,}[\w.\-]*", re.IGNORECASE)
_PATH_BEFORE_WORD = re.compile(rf"(?:{_PATH.pattern})\s+(?=dosya|klasör)", re.IGNORECASE)
PATH_PLACEHOLDER = "ekrandaki dosya"
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b")
_ARROWS = re.compile(r"\s*(?:→|->|=>|⇒)\s*")
_COLUMNS = re.compile(r"[ \t]{3,}")
_SPACES = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
# Harf harf okunan kısaltmalar (Türkçe harf adlarıyla)
_ACRONYMS = {
    "PDF": "pe de ef",
    "SMTP": "es em te pe",
    "LLM": "el el em",
    "STT": "es te te",
    "TTS": "te te es",
    "GPU": "ge pe u",
    "CPU": "se pe u",
    "ICS": "i ce es",
}
_ACRONYM = re.compile(r"\b(" + "|".join(_ACRONYMS) + r")\b")
MORE_ON_SCREEN = "Ayrıntılar ekranda."


def to_speech_text(text: str, max_chars: int = 500) -> str:
    """Konsol metninden seslendirilecek kopyayı üretir. Girdi değiştirilmez; Türkçe harfler korunur."""
    if not text:
        return ""
    spoken = text.replace("\r", "")
    spoken = _JARVIS.sub("Jarvis", spoken)
    spoken = _EMAIL.sub("e-posta adresi", spoken)
    spoken = _ISO_DATE.sub(lambda m: f"{int(m.group(3))}.{int(m.group(2))}.{m.group(1)}", spoken)
    spoken = _PATH_BEFORE_WORD.sub("", spoken)  # "logs/jarvis.log dosyasında" → "dosyasında"
    spoken = _PATH.sub(PATH_PLACEHOLDER, spoken)
    spoken = _ARROWS.sub(", ", spoken)
    spoken = _ACRONYM.sub(lambda m: _ACRONYMS[m.group(1)], spoken)
    lines = [line.strip() for line in spoken.split("\n") if line.strip()]
    lines = [_COLUMNS.sub(", ", line) for line in lines]
    spoken = " ".join(line if line[-1] in ".!?…:;," else f"{line}." for line in lines)
    spoken = _SPACES.sub(" ", spoken).strip()
    return shorten(spoken, max_chars)


def shorten(text: str, max_chars: int) -> str:
    """Uzun metinde baştan tam cümleler alınır; kalanı için ekrana yönlendirilir."""
    if len(text) <= max_chars:
        return text
    kept = ""
    for sentence in _SENTENCE.split(text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > max_chars:
            break
        kept = candidate
    if not kept:  # tek ve çok uzun cümle: kelime sınırında kes
        kept = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return f"{kept} {MORE_ON_SCREEN}"
