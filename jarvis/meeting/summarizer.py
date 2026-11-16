"""Kural tabanlı toplantı özetleri (G-06, kapanış özeti).

Yalnızca kayıtlı verilerden özet üretir; kayıtta olmayan karar, görev veya
kişi asla eklenmez. İsteğe bağlı LLM özeti (G-15) ileride ayrı bir sınıf
olarak eklenecek ve bu sınıfı yedek olarak kullanacak.
"""
from __future__ import annotations

from ..utils.dates import format_tr_date
from .models import ActionItem, ActionStatus, Meeting
from .speaker_tracker import SpeakerStat

MAX_SPOKEN_ITEMS = 5  # sesli okumada listelerin kesileceği uzunluk


def describe_action(action: ActionItem, with_date: bool = True) -> str:
    owner = action.owner or "sorumlu belirlenmedi"
    text = f"{owner}: {action.text}"
    if with_date:
        text += f", son tarih {format_tr_date(action.due_date, with_weekday=False)}"
    return text


def _limited(items: list[str], limit: int = MAX_SPOKEN_ITEMS) -> str:
    shown = items[:limit]
    text = "; ".join(f"{index}) {item}" for index, item in enumerate(shown, start=1))
    if len(items) > limit:
        text += f"; ve {len(items) - limit} madde daha"
    return text


class MeetingSummarizer:
    """Kural tabanlı özetleyici."""

    def previous_meeting_summary(self, previous: Meeting | None) -> str:
        if previous is None:
            return "Kayıtlı önceki toplantı bulunamadı."
        parts = [f"Önceki toplantı: {previous.title}, {format_tr_date(previous.started_at.date())}."]
        present = [p.name for p in previous.present_participants]
        if present:
            parts.append(f"Katılanlar: {', '.join(present)}.")
        if previous.decisions:
            parts.append(f"{len(previous.decisions)} karar alındı: "
                         f"{_limited([d.text for d in previous.decisions])}.")
        else:
            parts.append("Karar kaydı yok.")
        actions = previous.action_items
        if actions:
            done = [a for a in actions if a.status == ActionStatus.DONE]
            open_ = [a for a in actions if a.status == ActionStatus.OPEN]
            cancelled = [a for a in actions if a.status == ActionStatus.CANCELLED]
            counts = f"{len(actions)} görev verildi; tamamlanan {len(done)}, açık {len(open_)}"
            if cancelled:
                counts += f", iptal {len(cancelled)}"
            parts.append(counts + ".")
            if open_:
                parts.append(f"Açık görevler: {_limited([describe_action(a) for a in open_])}.")
        else:
            parts.append("Görev kaydı yok.")
        return " ".join(parts)

    def open_actions_summary(self, actions: list[ActionItem]) -> str:
        if not actions:
            return "Açık görev yok."
        lines = [describe_action(a) for a in actions]
        return f"{len(actions)} açık görev var: {_limited(lines, limit=10)}."

    def closing_summary(self, meeting: Meeting, stats: list[SpeakerStat], silent: list[str],
                        carryover_open: int) -> str:
        parts = []
        done_items = [i for i in meeting.agenda if i.status == "tamamlandı"]
        postponed = [i.title for i in meeting.agenda if i.status == "ertelendi"]
        if meeting.agenda:
            parts.append(f"Gündemdeki {len(meeting.agenda)} maddeden {len(done_items)} tanesi görüşüldü.")
        if postponed:
            parts.append(f"Ertelenen: {', '.join(postponed)}.")
        parts.append(f"{len(meeting.decisions)} karar ve {len(meeting.action_items)} görev kaydedildi.")
        if meeting.votes:
            parts.append(f"{len(meeting.votes)} oylama yapıldı.")
        if meeting.parking_lot:
            parts.append(f"Otoparkta {len(meeting.parking_lot)} konu var.")
        if meeting.carryover_completed:
            parts.append(f"Önceki toplantılardan {len(meeting.carryover_completed)} görev bu toplantıda kapatıldı.")
        if carryover_open:
            parts.append(f"Önceki toplantılardan devreden {carryover_open} açık görev var.")
        if stats and stats[0].total_seconds >= 1:
            top = stats[0]
            parts.append(f"En uzun söz süresi: {top.name} ({top.minutes_text}).")
        if silent:
            parts.append(f"Söz almayan katılımcılar: {', '.join(silent)}.")
        missing = [a for a in meeting.action_items if not a.owner or a.due_date is None]
        if missing:
            parts.append(f"Dikkat: {len(missing)} görevin sorumlusu veya son tarihi eksik.")
        return " ".join(parts)
