"""Kural tabanlı özetler: yalnızca kayıtlı veriden üretilir."""
from datetime import date, datetime

from jarvis.meeting.models import ActionItem, ActionStatus, AgendaItem, Decision, Meeting, Participant
from jarvis.meeting.speaker_tracker import SpeakerStat
from jarvis.meeting.summarizer import MeetingSummarizer

T0 = datetime(2026, 10, 7, 10, 0, 0)


def previous_meeting() -> Meeting:
    return Meeting(
        title="Haftalık Toplantı", started_at=T0,
        participants=[Participant("Ayşe Yılmaz", present=True), Participant("Mehmet Öztürk", present=True),
                      Participant("Ahmet Yılmaz", present=False)],
        decisions=[Decision("Plan A uygulanacak"), Decision("Bütçe onaylandı"), Decision("Vardiya değişecek")],
        action_items=[
            ActionItem("Rapor", "Mehmet Öztürk", date(2026, 10, 14), status=ActionStatus.DONE),
            ActionItem("Sunum", "Ayşe Yılmaz", date(2026, 10, 9), status=ActionStatus.DONE),
            ActionItem("Tedarikçi görüşmesi", "", None),
        ],
    )


def test_previous_summary_contents():
    text = MeetingSummarizer().previous_meeting_summary(previous_meeting())
    assert "Haftalık Toplantı" in text and "7 Ekim 2026" in text
    assert "Ayşe Yılmaz, Mehmet Öztürk" in text and "Ahmet" not in text
    assert "3 karar alındı" in text and "Bütçe onaylandı" in text
    assert "tamamlanan 2, açık 1" in text
    assert "sorumlu belirlenmedi: Tedarikçi görüşmesi" in text


def test_previous_summary_none():
    assert "bulunamadı" in MeetingSummarizer().previous_meeting_summary(None)


def test_previous_summary_without_records_does_not_invent():
    meeting = Meeting(title="Boş", started_at=T0)
    text = MeetingSummarizer().previous_meeting_summary(meeting)
    assert "Karar kaydı yok" in text and "Görev kaydı yok" in text


def test_long_lists_are_truncated_for_speech():
    meeting = previous_meeting()
    meeting.decisions = [Decision(f"Karar {i}") for i in range(9)]
    text = MeetingSummarizer().previous_meeting_summary(meeting)
    assert "ve 4 madde daha" in text


def test_closing_summary():
    meeting = Meeting(title="T", agenda=[AgendaItem("A", status="tamamlandı"), AgendaItem("B", status="ertelendi")])
    meeting.action_items = [ActionItem("X", "", None)]
    text = MeetingSummarizer().closing_summary(meeting, [SpeakerStat("Ayşe Yılmaz", 120, 2)], ["Mehmet Öztürk"], 2)
    assert "2 maddeden 1 tanesi görüşüldü" in text and "Ertelenen: B" in text
    assert "En uzun söz süresi: Ayşe Yılmaz (2 dakika)" in text  # canlı düzeltme: söz süresi
    assert "Söz almayan katılımcılar: Mehmet Öztürk" in text
    assert "devreden 2 açık görev" in text
    assert "1 görevin sorumlusu veya son tarihi eksik" in text


def test_open_actions_summary():
    summarizer = MeetingSummarizer()
    assert summarizer.open_actions_summary([]) == "Açık görev yok."
    assert "1 açık görev var" in summarizer.open_actions_summary([ActionItem("Rapor", "Ayşe")])
