"""Transkript servisi (G-09).

Transkriptin tek kaynağı `Meeting.transcript` listesidir. Her kayıt zaman
damgası, konuşmacı ve o anki gündem maddesini taşır. Konuşmacı bilinmiyorsa
rastgele bir kişi atanmaz; "Belirtilmemiş" yazılır.
"""
from __future__ import annotations

from datetime import datetime

from ..utils.turkish_text import normalize_spaces
from .models import Meeting, TranscriptEntry

UNKNOWN_SPEAKER = "Belirtilmemiş"
# Tek bir STT segmenti için üst sınır; bunu aşan metin bölünerek kaydedilir (PDF'te okunabilirlik için).
MAX_ENTRY_CHARS = 2000


class TranscriptService:
    def __init__(self, meeting: Meeting):
        self.meeting = meeting

    def add(self, text: str, at: datetime, speaker: str | None = None, kind: str = "",
            duration_s: float | None = None) -> list[TranscriptEntry]:
        """Metni transkripte ekler; boş metin eklenmez."""
        clean = normalize_spaces(text)
        if not clean:
            return []
        who = speaker or self.meeting.active_speaker or UNKNOWN_SPEAKER
        item = self.meeting.current_item
        entries = []
        chunks = _split_long(clean)
        for chunk in chunks:
            share = duration_s * len(chunk) / len(clean) if duration_s is not None and len(chunks) > 1 else duration_s
            entry = TranscriptEntry(timestamp=at, speaker=who, text=chunk, agenda_item=item.title if item else "",
                                    kind=kind, duration_s=share)
            self.meeting.transcript.append(entry)
            entries.append(entry)
        return entries

    def entries_for(self, speaker: str) -> list[TranscriptEntry]:
        return [entry for entry in self.meeting.transcript if entry.speaker == speaker]

    @property
    def is_empty(self) -> bool:
        return not self.meeting.transcript


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_ENTRY_CHARS:
        return [text]
    chunks: list[str] = []
    current = ""
    for word in text.split(" "):
        if current and len(current) + 1 + len(word) > MAX_ENTRY_CHARS:
            chunks.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        chunks.append(current)
    return chunks
