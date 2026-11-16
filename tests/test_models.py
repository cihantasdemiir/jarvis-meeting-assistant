"""Veri modeli JSON serileştirme / geri yükleme testleri."""
import json
from datetime import date, datetime

from jarvis.meeting.models import (
    ActionItem,
    AgendaItem,
    Decision,
    Meeting,
    Participant,
    SpeakerTurn,
    TranscriptEntry,
    Vote,
    VoteResult,
)


def build_full_meeting() -> Meeting:
    t0 = datetime(2026, 10, 14, 10, 0, 0)
    return Meeting(
        title="Çalışma Toplantısı — Şubat İçin Öneriler",
        started_at=t0,
        ended_at=datetime(2026, 10, 14, 11, 0, 0),
        chair="Kenan Çetin",
        location="İstanbul Ofis",
        participants=[Participant("Ayşe Yılmaz", "ayse@example.com", "Planlama", True)],
        agenda=[AgendaItem("Üretim planlaması", 15, "Ayşe Yılmaz", "tamamlandı", t0, t0, ["not ğüşiöç"])],
        decisions=[Decision("Yeni plan pazartesi uygulanacak", "Üretim planlaması", t0)],
        action_items=[ActionItem("Raporu hazırla", "Mehmet Öztürk", date(2026, 10, 21), meeting_id="m1")],
        votes=[Vote("Bütçe", 5, 2, 1)],
        parking_lot=["Otopark konusu"],
        notes=["Genel not"],
        speaker_turns=[SpeakerTurn("Ayşe Yılmaz", t0, datetime(2026, 10, 14, 10, 5), "Üretim planlaması")],
        transcript=[TranscriptEntry(t0, "Ayşe Yılmaz", "Merhaba, İŞÇİ ılık şükür", "Üretim planlaması")],
        previous_meeting_id="önceki",
        next_meeting_date=date(2026, 10, 21),
        summary="Özet",
    )


def test_meeting_roundtrip_through_json():
    meeting = build_full_meeting()
    restored = Meeting.from_dict(json.loads(json.dumps(meeting.to_dict(), ensure_ascii=False)))
    assert restored.to_dict() == meeting.to_dict()
    assert restored.transcript[0].text == "Merhaba, İŞÇİ ılık şükür"
    assert restored.action_items[0].due_date == date(2026, 10, 21)


def test_vote_result():
    assert Vote("a", 5, 2).result == VoteResult.ACCEPTED
    assert Vote("a", 1, 3).result == VoteResult.REJECTED
    assert Vote("a", 2, 2, 4).result == VoteResult.TIE


def test_speaker_turn_duration():
    turn = SpeakerTurn("A", datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 10, 2, 30))
    assert turn.duration_seconds == 150
    assert SpeakerTurn("A", datetime(2026, 1, 1, 10)).duration_seconds == 0


def test_action_items_get_unique_ids():
    ids = {ActionItem("x").id for _ in range(200)}
    assert len(ids) == 200
