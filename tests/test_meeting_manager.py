"""MeetingManager: yaşam döngüsü, yoklama ve gündem kuralları."""
import pytest

from jarvis.meeting.manager import MeetingManager, NoActiveMeeting
from jarvis.meeting.models import AgendaStatus, MeetingStatus


def test_start_meeting_loads_files(manager):
    result = manager.start_meeting()
    assert result.ok
    meeting = manager.meeting
    assert meeting.title == "Test Toplantısı"
    assert meeting.chair == "Kenan Çetin" and meeting.location == "Oda 1"
    assert len(meeting.participants) == 4 and len(meeting.agenda) == 3
    assert meeting.agenda[0].status == AgendaStatus.ACTIVE
    assert "3 madde" in result.message and "Üretim planlaması" in result.message


def test_start_with_custom_title(manager):
    manager.start_meeting("Bütçe   Toplantısı")
    assert manager.meeting.title == "Bütçe Toplantısı"


def test_cannot_start_two_meetings(manager):
    manager.start_meeting()
    assert not manager.start_meeting().ok


def test_start_without_agenda_file_does_not_crash(manager, settings):
    """T-02: gündem dosyası yokken 'Gündem boş' uyarısı."""
    settings.agenda_file.unlink()
    result = manager.start_meeting()
    assert result.ok
    assert "Gündem boş" in result.message
    assert any("Gündem dosyası bulunamadı" in w for w in result.data["warnings"])


def test_start_with_corrupt_participants(manager, settings):
    settings.participants_file.write_text("{bozuk", encoding="utf-8")
    result = manager.start_meeting()
    assert result.ok and manager.meeting.participants == []
    assert manager.mark_present(["Ayşe"]).data["added"] == ["Ayşe"]


def test_operations_require_active_meeting(manager):
    with pytest.raises(NoActiveMeeting):
        manager.read_agenda()


def test_end_meeting_and_double_end(manager, clock):
    manager.start_meeting()
    clock.advance(minutes=42)
    result = manager.end_meeting()
    assert result.ok and result.data["duration_minutes"] == 42
    assert manager.meeting.status == MeetingStatus.ENDED
    assert manager.meeting.agenda[0].status == AgendaStatus.DONE
    second = manager.end_meeting()
    assert not second.ok and "zaten" in second.message


def test_every_change_is_persisted(manager, repo):
    manager.start_meeting()
    manager.mark_present(["Ayşe"])
    stored = repo.get(manager.meeting.id)
    assert stored.find_participant("Ayşe Yılmaz").present


def test_recover_active_meeting(manager, repo, settings, clock):
    manager.start_meeting()
    manager.mark_present(["Mehmet"])
    meeting_id = manager.meeting.id
    # Program kapandı ve yeniden açıldı
    fresh = MeetingManager(repo, settings, clock=clock)
    result = fresh.recover_active_meeting()
    assert result is not None and fresh.meeting.id == meeting_id
    assert fresh.meeting.find_participant("Mehmet Öztürk").present


def test_mark_present_single_and_many(manager):
    manager.start_meeting()
    result = manager.mark_present(["Kenan", "Ayşe", "Mehmet"])
    assert result.ok and result.data["marked"] == ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk"]
    assert "3 katılımcının yoklaması alındı" in result.message


def test_mark_present_splits_comma_less_stt_output(manager):
    manager.start_meeting()
    result = manager.mark_present(["Kenan Ayşe", "Mehmet"])
    assert result.data["marked"] == ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk"]
    assert result.data["added"] == []


def test_mark_present_no_duplicates(manager):
    manager.start_meeting()
    manager.mark_present(["Ayşe"])
    result = manager.mark_present(["Ayşe Yılmaz"])
    assert result.data["already"] == ["Ayşe Yılmaz"]
    assert sum(1 for p in manager.meeting.participants if p.name == "Ayşe Yılmaz") == 1


def test_mark_present_unknown_added_once(manager):
    manager.start_meeting()
    manager.mark_present(["zeynep kaya"])
    manager.mark_present(["Zeynep Kaya"])
    added = [p for p in manager.meeting.participants if p.added_during_meeting]
    assert [p.name for p in added] == ["Zeynep Kaya"]


def test_mark_present_ambiguous_surname_not_guessed(manager):
    manager.start_meeting()
    result = manager.mark_present(["Yılmaz"])
    assert not result.ok
    assert "Yılmaz" in result.data["ambiguous"]
    assert not any(p.present for p in manager.meeting.participants)


def test_attendance_status(manager):
    manager.start_meeting()
    manager.mark_present(["Ayşe", "Mehmet"])
    result = manager.attendance_status()
    assert result.data["present"] == ["Ayşe Yılmaz", "Mehmet Öztürk"]
    assert result.data["absent"] == ["Kenan Çetin", "Ahmet Yılmaz"]


def test_agenda_navigation(manager, clock):
    manager.start_meeting()
    clock.advance(minutes=5)
    assert manager.next_item().data["title"] == "Kalite raporu"
    agenda = manager.meeting.agenda
    assert agenda[0].status == AgendaStatus.DONE and agenda[0].elapsed_seconds == 300
    assert manager.previous_item().data["title"] == "Üretim planlaması"
    assert agenda[1].status == AgendaStatus.WAITING
    assert agenda[0].status == AgendaStatus.ACTIVE
    assert manager.next_item().data["title"] == "Kalite raporu"
    assert manager.next_item().data["title"] == "Diğer konular"
    final = manager.next_item()
    assert final.data["finished"] and "başka madde yok" in final.message


def test_previous_at_first_item(manager):
    manager.start_meeting()
    assert not manager.previous_item().ok


def test_skip_item_marks_postponed(manager):
    manager.start_meeting()
    result = manager.skip_item()
    assert manager.meeting.agenda[0].status == AgendaStatus.POSTPONED
    assert result.data["next"] == "Kalite raporu"
    manager.next_item()
    final = manager.next_item()
    assert "Ertelenen maddeler: Üretim planlaması" in final.message


def test_add_agenda_item(manager):
    manager.start_meeting()
    result = manager.add_agenda_item("Yeni personel", 15)
    assert result.ok and manager.meeting.agenda[-1].planned_minutes == 15
    default = manager.add_agenda_item("Varsayılan süre")
    assert default.data["minutes"] == manager.settings.default_item_minutes
    assert not manager.add_agenda_item("  ").ok


def test_add_item_to_empty_agenda_activates_it(manager, settings):
    settings.agenda_file.unlink()
    manager.start_meeting()
    manager.add_agenda_item("İlk madde", 5)
    assert manager.meeting.current_item.title == "İlk madde"


def test_read_agenda(manager):
    manager.start_meeting()
    result = manager.read_agenda()
    assert result.data["items"] == ["Üretim planlaması", "Kalite raporu", "Diğer konular"]
    assert "sunan: Ayşe Yılmaz" in result.message


def test_transcript_unknown_speaker_not_invented(manager):
    manager.start_meeting()
    manager.add_transcript("Bu hafta iki hat bakımda.")
    entry = manager.meeting.transcript[0]
    assert entry.speaker == "Belirtilmemiş" and entry.agenda_item == "Üretim planlaması"


def test_previous_meeting_link(manager, repo, settings, clock):
    manager.start_meeting()
    first_id = manager.meeting.id
    manager.end_meeting()
    clock.advance(days=7)
    second = MeetingManager(repo, settings, clock=clock)
    second.start_meeting()
    assert second.meeting.previous_meeting_id == first_id
