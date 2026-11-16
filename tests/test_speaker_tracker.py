"""Konuşmacı takibi: tur açma/kapama, süre ve söz alma sayısı."""
from datetime import datetime, timedelta

from jarvis.meeting.models import Meeting, Participant
from jarvis.meeting.speaker_tracker import SpeakerTracker

T0 = datetime(2026, 10, 14, 10, 0, 0)


def make_meeting() -> Meeting:
    return Meeting(title="T", started_at=T0, participants=[
        Participant("Ayşe Yılmaz", present=True), Participant("Mehmet Öztürk", present=True),
        Participant("Kenan Çetin", present=True), Participant("Ahmet Yılmaz", present=False)])


def test_switching_speaker_closes_previous_turn():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    assert tracker.set_speaker("Ayşe Yılmaz", T0, "Madde 1")
    assert tracker.set_speaker("Mehmet Öztürk", T0 + timedelta(minutes=3))
    first, second = meeting.speaker_turns
    assert first.ended_at == T0 + timedelta(minutes=3) and first.agenda_item == "Madde 1"
    assert second.ended_at is None
    assert meeting.active_speaker == "Mehmet Öztürk"


def test_same_speaker_does_not_open_new_turn():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    tracker.set_speaker("Ayşe Yılmaz", T0)
    assert tracker.set_speaker("Ayşe Yılmaz", T0 + timedelta(minutes=1)) is False
    assert len(meeting.speaker_turns) == 1


def test_stats_totals_and_turn_counts():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    tracker.set_speaker("Ayşe Yılmaz", T0)
    tracker.set_speaker("Mehmet Öztürk", T0 + timedelta(minutes=2))
    tracker.set_speaker("Ayşe Yılmaz", T0 + timedelta(minutes=3))
    tracker.close_active(T0 + timedelta(minutes=6))
    stats = {s.name: s for s in tracker.stats()}
    assert stats["Ayşe Yılmaz"].total_seconds == 300 and stats["Ayşe Yılmaz"].turns == 2
    assert stats["Mehmet Öztürk"].total_seconds == 60 and stats["Mehmet Öztürk"].turns == 1
    assert tracker.stats()[0].name == "Ayşe Yılmaz"
    assert meeting.active_speaker is None


def test_open_turn_counted_until_now():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    tracker.set_speaker("Ayşe Yılmaz", T0)
    assert tracker.stats(now=T0 + timedelta(seconds=90))[0].total_seconds == 90
    assert tracker.stats()[0].total_seconds == 0  # "şimdi" verilmezse açık tur sayılmaz


def test_silent_participants_only_present_people():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    tracker.set_speaker("Ayşe Yılmaz", T0)
    assert tracker.silent_participants() == ["Mehmet Öztürk", "Kenan Çetin"]


def test_close_before_start_never_negative():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    tracker.set_speaker("Ayşe Yılmaz", T0)
    tracker.close_active(T0 - timedelta(minutes=5))  # saat geri alınmış olsa bile
    assert meeting.speaker_turns[0].duration_seconds == 0


def test_minutes_text():
    meeting = make_meeting()
    tracker = SpeakerTracker(meeting)
    tracker.set_speaker("Ayşe Yılmaz", T0)
    tracker.close_active(T0 + timedelta(seconds=150))
    assert tracker.stats()[0].minutes_text == "2 dakika 30 saniye"
