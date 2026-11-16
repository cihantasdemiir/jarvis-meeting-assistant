"""Transkript servisi ve T-03."""
from datetime import datetime

from jarvis.meeting.models import AgendaItem, Meeting
from jarvis.meeting.transcript_service import MAX_ENTRY_CHARS, UNKNOWN_SPEAKER, TranscriptService

T0 = datetime(2026, 10, 14, 10, 0, 0)


def test_t03_three_sentences_recorded_for_ayse(assistant, repo):
    """T-03: 'söz Ayşe'de' ardından 3 cümle -> transkriptte 3 satır Ayşe Yılmaz adına."""
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis, söz Ayşe'de")
    for sentence in ["Birinci cümle.", "İkinci cümle.", "Üçüncü cümle."]:
        assistant.handle_text(sentence)
    stored = repo.get(assistant.manager.meeting.id)
    ayse_lines = [e for e in stored.transcript if e.speaker == "Ayşe Yılmaz"]
    assert [e.text for e in ayse_lines] == ["Birinci cümle.", "İkinci cümle.", "Üçüncü cümle."]
    assert len(stored.transcript) == 3
    assert all(e.agenda_item == "Üretim planlaması" for e in ayse_lines)


def test_commands_are_not_written_to_transcript(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis karar: Plan onaylandı")
    assistant.handle_text("Jarvis söz Mehmet'te")
    assert assistant.manager.meeting.transcript == []


def test_speaker_change_mid_meeting(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Önce kimse söz almadı.")
    assistant.handle_text("Jarvis söz Mehmet'te")
    assistant.handle_text("Mehmet konuşuyor.")
    assistant.handle_text("Jarvis sözü bırak")
    assistant.handle_text("Yine sahipsiz.")
    speakers = [e.speaker for e in assistant.manager.meeting.transcript]
    assert speakers == [UNKNOWN_SPEAKER, "Mehmet Öztürk", UNKNOWN_SPEAKER]


def test_service_adds_timestamp_and_item():
    meeting = Meeting(title="T", agenda=[AgendaItem("Bütçe")], current_item_index=0)
    entries = TranscriptService(meeting).add("  Merhaba   dünya ", T0, speaker="Ayşe Yılmaz")
    assert entries[0].text == "Merhaba dünya" and entries[0].timestamp == T0
    assert entries[0].agenda_item == "Bütçe"


def test_empty_text_ignored():
    meeting = Meeting(title="T")
    assert TranscriptService(meeting).add("   ", T0) == []
    assert TranscriptService(meeting).is_empty


def test_long_segment_split_without_losing_words():
    meeting = Meeting(title="T")
    words = [f"kelime{i}" for i in range(1500)]
    entries = TranscriptService(meeting).add(" ".join(words), T0)
    assert len(entries) > 1
    assert all(len(e.text) <= MAX_ENTRY_CHARS for e in entries)
    assert " ".join(e.text for e in entries).split() == words


def test_long_meeting_transcript_persists(manager, repo, clock):
    manager.start_meeting()
    for i in range(500):
        clock.advance(seconds=10)
        manager.add_transcript(f"Satır {i} — çok uzun bir toplantıda Türkçe karakterler: ğüşiöç")
    stored = repo.get(manager.meeting.id)
    assert len(stored.transcript) == 500
    assert stored.transcript[-1].text.startswith("Satır 499")
