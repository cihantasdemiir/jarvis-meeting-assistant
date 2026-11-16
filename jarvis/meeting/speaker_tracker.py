"""Konuşmacı takibi (G-07).

Aktif konuşmacı sesli komutla belirlenir ("Söz Ayşe'de"). Söz başka birine
geçtiğinde önceki konuşmacının turu kapatılır. Toplantı bitince açık tur
kapatılır. İstatistikler `Meeting.speaker_turns` listesinden hesaplanır;
ayrı bir durum tutulmaz, bu yüzden kurtarılan toplantılarda da doğrudur.

Ölçümün anlamı (Faz 5c.2 canlı düzeltme): `total_seconds` SÖZ SÜRESİDİR — "söz X'te" komutundan söz başkasına
geçene veya kapatılana kadar geçen süre. Kişinin gerçekten konuştuğu süre değildir; sessizlik ve asistanın
yanıtları da içindedir. Gerçek konuşmaya yakın ölçü `segments` (kişi adına transkripte yazılan konuşma bölümü)
ve sesli girişte `speech_seconds` (bu bölümlerin ses süresi) alanlarıdır; komut kalıpları sayılmaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Meeting, SpeakerTurn


@dataclass
class SpeakerStat:
    name: str
    total_seconds: float  # söz süresi
    turns: int
    segments: int = 0  # kayda geçen konuşma bölümü sayısı
    speech_seconds: float | None = None  # bu bölümlerin ses süresi (yalnızca sesli girişte bilinir)

    @property
    def speech_text(self) -> str:
        """Kayda geçen konuşma: "3 bölüm, 42 saniye ses" / "3 bölüm"."""
        base = f"{self.segments} bölüm"
        if self.speech_seconds is None or self.segments == 0:
            return base
        return f"{base}, {_duration_text(self.speech_seconds)} ses"

    @property
    def minutes_text(self) -> str:
        return _duration_text(self.total_seconds)


def _duration_text(total: float) -> str:
    minutes, seconds = divmod(int(round(total)), 60)
    if minutes == 0:
        return f"{seconds} saniye"
    return f"{minutes} dakika {seconds} saniye" if seconds else f"{minutes} dakika"


class SpeakerTracker:
    def __init__(self, meeting: Meeting):
        self.meeting = meeting

    @property
    def active_turn(self) -> SpeakerTurn | None:
        for turn in reversed(self.meeting.speaker_turns):
            if turn.ended_at is None:
                return turn
        return None

    @property
    def active_speaker(self) -> str | None:
        return self.meeting.active_speaker

    def set_speaker(self, name: str, at: datetime, agenda_item: str = "") -> bool:
        """Sözü verir. Aynı kişi zaten konuşuyorsa yeni tur açılmaz (False döner)."""
        current = self.active_turn
        if current is not None and current.speaker == name:
            self.meeting.active_speaker = name
            return False
        self.close_active(at)
        self.meeting.speaker_turns.append(SpeakerTurn(speaker=name, started_at=at, agenda_item=agenda_item))
        self.meeting.active_speaker = name
        return True

    def close_active(self, at: datetime) -> SpeakerTurn | None:
        """Açık turları kapatır (normalde en fazla bir tane olur)."""
        closed = None
        for turn in self.meeting.speaker_turns:
            if turn.ended_at is None:
                turn.ended_at = max(at, turn.started_at)
                closed = turn
        self.meeting.active_speaker = None
        return closed

    def stats(self, now: datetime | None = None) -> list[SpeakerStat]:
        """Kişi başı toplam süre ve söz alma sayısı (süreye göre azalan)."""
        totals: dict[str, SpeakerStat] = {}
        for turn in self.meeting.speaker_turns:
            stat = totals.setdefault(turn.speaker, SpeakerStat(turn.speaker, 0.0, 0))
            stat.turns += 0 if turn.resumed else 1
            end = turn.ended_at or now
            if end is not None:
                stat.total_seconds += max(0.0, (end - turn.started_at).total_seconds())
        for entry in self.meeting.transcript:
            stat = totals.get(entry.speaker)
            if stat is None or not entry.is_speech:
                continue
            stat.segments += 1
            if entry.duration_s is not None:
                stat.speech_seconds = (stat.speech_seconds or 0.0) + entry.duration_s
        return sorted(totals.values(), key=lambda s: (-s.total_seconds, s.name))

    def silent_participants(self) -> list[str]:
        """Toplantıya katılan ama hiç söz almayan kişiler."""
        spoke = {turn.speaker for turn in self.meeting.speaker_turns}
        return [p.name for p in self.meeting.present_participants if p.name not in spoke]
