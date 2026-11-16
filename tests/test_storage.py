"""SQLite kaydet/yükle, devreden görev sorguları ve kurtarma testleri."""
from datetime import date, datetime

import pytest

from jarvis.meeting.models import ActionItem, ActionStatus, Meeting, MeetingStatus
from jarvis.meeting.storage import MeetingRepository, StorageError


def make_meeting(title: str, started: datetime, status: str = MeetingStatus.ENDED, demo: bool = False) -> Meeting:
    meeting = Meeting(title=title, started_at=started, status=status, is_demo=demo)
    return meeting


def test_save_and_load(repo):
    meeting = make_meeting("Şube Toplantısı", datetime(2026, 10, 1, 9))
    meeting.action_items.append(ActionItem("Rapor", "Ayşe", date(2026, 10, 8), meeting_id=meeting.id))
    repo.save(meeting)
    loaded = repo.get(meeting.id)
    assert loaded is not None
    assert loaded.to_dict() == meeting.to_dict()


def test_save_is_idempotent_and_updates(repo):
    meeting = make_meeting("A", datetime(2026, 10, 1, 9), MeetingStatus.ACTIVE)
    repo.save(meeting)
    meeting.title = "B"
    repo.save(meeting)
    assert repo.get(meeting.id).title == "B"
    assert len(repo.list_meetings()) == 1


def test_get_active_returns_unfinished_meeting(repo):
    repo.save(make_meeting("Bitmiş", datetime(2026, 10, 1, 9)))
    active = make_meeting("Yarım", datetime(2026, 10, 2, 9), MeetingStatus.ACTIVE)
    repo.save(active)
    assert repo.get_active().id == active.id


def test_demo_meetings_are_isolated(repo):
    repo.save(make_meeting("Demo", datetime(2026, 10, 3, 9), MeetingStatus.ACTIVE, demo=True))
    assert repo.get_active() is None
    assert repo.get_active(include_demo=True) is not None
    assert repo.list_meetings() == []


def test_get_last_ended(repo):
    old = make_meeting("Eski", datetime(2026, 9, 1, 9))
    new = make_meeting("Yeni", datetime(2026, 10, 1, 9))
    repo.save(old)
    repo.save(new)
    repo.save(make_meeting("Aktif", datetime(2026, 10, 10, 9), MeetingStatus.ACTIVE))
    assert repo.get_last_ended().title == "Yeni"
    assert repo.get_last_ended(exclude_id=new.id).title == "Eski"


def test_open_actions_and_status_update(repo):
    meeting = make_meeting("M1", datetime(2026, 10, 1, 9))
    first = ActionItem("Rapor", "Mehmet", date(2026, 10, 8), meeting_id=meeting.id)
    second = ActionItem("Sunum", "Ayşe", None, meeting_id=meeting.id)
    meeting.action_items = [first, second]
    repo.save(meeting)
    assert [a.id for a in repo.get_open_actions()] == [first.id, second.id]

    updated = repo.update_action_status(first.id, ActionStatus.DONE)
    assert updated.status == ActionStatus.DONE and updated.completed_at is not None
    assert [a.id for a in repo.get_open_actions()] == [second.id]
    # Toplantı JSON'u da güncellenmiş olmalı
    reloaded = repo.get(meeting.id)
    assert reloaded.action_items[0].status == ActionStatus.DONE


def test_removed_action_is_deleted_from_table(repo):
    meeting = make_meeting("M1", datetime(2026, 10, 1, 9))
    action = ActionItem("Rapor", meeting_id=meeting.id)
    meeting.action_items = [action]
    repo.save(meeting)
    meeting.action_items = []
    repo.save(meeting)
    assert repo.get_open_actions() == []


def test_actions_due_between(repo):
    meeting = make_meeting("M1", datetime(2026, 10, 1, 9))
    meeting.action_items = [ActionItem("A", due_date=date(2026, 10, 15), meeting_id=meeting.id),
                            ActionItem("B", due_date=date(2026, 11, 15), meeting_id=meeting.id)]
    repo.save(meeting)
    due = repo.get_actions_due_between(date(2026, 10, 14), date(2026, 10, 20))
    assert [a.text for a in due] == ["A"]


def test_data_survives_new_repository_instance(settings):
    first = MeetingRepository(settings.db_path)
    meeting = make_meeting("Kalıcı", datetime(2026, 10, 1, 9), MeetingStatus.ACTIVE)
    first.save(meeting)
    second = MeetingRepository(settings.db_path)  # program yeniden başlatıldı
    assert second.get_active().title == "Kalıcı"


def test_corrupt_database_reports_clear_error(tmp_path):
    bad = tmp_path / "bozuk.db"
    bad.write_bytes(b"bu bir sqlite dosyasi degil" * 100)
    with pytest.raises(StorageError):
        MeetingRepository(bad)
