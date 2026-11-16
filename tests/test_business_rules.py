"""Faz 2 iş kuralları: karar, görev, devreden görev, oylama, otopark, süre, özet."""
from datetime import date

from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.models import ActionStatus, VoteResult

MEETING_DAY = date(2026, 10, 14)


def second_meeting(repo, settings, clock):
    clock.advance(days=7)
    return MeetingManager(repo, settings, clock=clock)


# ------------------------------------------------------------------ karar / görev
def test_add_decision_linked_to_agenda_item(manager, repo):
    manager.start_meeting()
    result = manager.add_decision("Yeni üretim planı pazartesi uygulanacak.")
    assert result.ok
    stored = repo.get(manager.meeting.id)
    assert stored.decisions[0].text == "Yeni üretim planı pazartesi uygulanacak"
    assert stored.decisions[0].agenda_item == "Üretim planlaması"


def test_t04_action_owner_and_date(manager, repo):
    manager.start_meeting()
    result = manager.add_action("Mehmet raporu haftaya kadar hazırlasın")
    action = repo.get(manager.meeting.id).action_items[0]
    assert action.owner == "Mehmet Öztürk"
    assert action.due_date == date(2026, 10, 21)  # bugün + 7
    assert action.text == "raporu hazırlasın"
    assert action.status == ActionStatus.OPEN and action.agenda_item == "Üretim planlaması"
    assert "Sorumlu: Mehmet Öztürk" in result.message and "21 Ekim 2026" in result.message


def test_action_missing_owner_asks_for_confirmation(manager):
    manager.start_meeting()
    result = manager.add_action("tedarikçiyle görüşülsün")
    assert "Sorumlu anlaşılamadı" in result.message and "Son tarih anlaşılamadı" in result.message
    assert manager.set_last_action_owner("Ayşe").ok
    assert manager.set_last_action_due("cumaya kadar").ok
    action = manager.meeting.action_items[0]
    assert action.owner == "Ayşe Yılmaz" and action.due_date == date(2026, 10, 16)


def test_ambiguous_owner_not_assigned(manager):
    manager.start_meeting()
    result = manager.add_action("Yılmaz sunumu yarın hazırlasın")
    assert manager.meeting.action_items[0].owner == ""
    assert "Sorumlu belirsiz" in result.message
    ambiguous = manager.set_last_action_owner("Yılmaz")
    assert not ambiguous.ok
    assert manager.set_last_action_owner("Ahmet Yılmaz").ok


def test_owner_outside_list_only_when_explicit(manager):
    manager.start_meeting()
    manager.add_action("satın alma teklif toplasın")
    result = manager.set_last_action_owner("satın alma birimi")
    assert result.ok and "katılımcı listesinde yok" in result.message
    assert manager.meeting.action_items[0].owner == "Satın Alma Birimi"


def test_bad_due_date_rejected(manager):
    manager.start_meeting()
    manager.add_action("Ayşe rapor yazsın")
    assert not manager.set_last_action_due("yakında").ok
    assert manager.meeting.action_items[0].due_date is None


def test_owner_due_without_action(manager):
    manager.start_meeting()
    assert not manager.set_last_action_owner("Ayşe").ok
    assert not manager.set_last_action_due("yarın").ok


# ------------------------------------------------------------------ devreden görevler (G-13, T-08, T-09)
def test_t08_previous_meeting_summary(manager, repo, settings, clock):
    manager.start_meeting()
    manager.mark_present(["Ayşe", "Mehmet"])
    manager.add_decision("Plan A uygulanacak")
    manager.add_action("Mehmet raporu haftaya kadar hazırlasın")
    manager.end_meeting()

    second = second_meeting(repo, settings, clock)
    start = second.start_meeting()
    assert start.data["carryover_count"] == 1
    summary = second.summarize_previous()
    assert summary.ok
    assert "Plan A uygulanacak" in summary.message
    assert "Mehmet Öztürk: raporu hazırlasın" in summary.message
    assert "Ayşe Yılmaz, Mehmet Öztürk" in summary.message


def test_summarize_previous_when_none(manager):
    manager.start_meeting()
    result = manager.summarize_previous()
    assert not result.ok and "bulunamadı" in result.message


def test_t09_completed_carryover_disappears(manager, repo, settings, clock):
    manager.start_meeting()
    manager.add_action("Mehmet raporu haftaya kadar hazırlasın")
    manager.add_action("Ayşe sunumu cumaya kadar hazırlasın")
    first_id = manager.meeting.id
    manager.end_meeting()

    second = second_meeting(repo, settings, clock)
    second.start_meeting()
    assert len(second.all_open_actions()) == 2
    result = second.complete_action("rapor")
    assert result.ok, result.message
    open_texts = [a.text for a in second.all_open_actions()]
    assert open_texts == ["sunumu hazırlasın"]
    assert repo.get(first_id).action_items[0].status == ActionStatus.DONE
    assert second.meeting.carryover_completed == [result.data["action_id"]]
    assert [a.text for a in repo.get_open_actions(exclude_meeting_id=second.meeting.id)] == ["sunumu hazırlasın"]

    second.end_meeting()
    third = second_meeting(repo, settings, clock)
    third.start_meeting()
    assert "tamamlanan 0" not in third.summarize_previous().message


def test_complete_current_meeting_action_by_number(manager):
    manager.start_meeting()
    manager.add_action("Ayşe sunumu yarın hazırlasın")
    manager.add_action("Mehmet raporu yarın yazsın")
    assert manager.complete_action("2").ok
    assert [a.status for a in manager.meeting.action_items] == [ActionStatus.OPEN, ActionStatus.DONE]


def test_complete_action_ambiguous_and_missing(manager):
    manager.start_meeting()
    manager.add_action("Ayşe bütçe raporu hazırlasın")
    manager.add_action("Mehmet kalite raporu hazırlasın")
    ambiguous = manager.complete_action("rapor")
    assert not ambiguous.ok and len(ambiguous.data["candidates"]) == 2
    assert manager.complete_action("kalite raporu").ok
    assert not manager.complete_action("olmayan iş").ok
    assert not manager.complete_action("99").ok


def test_complete_by_owner_name(manager):
    manager.start_meeting()
    manager.add_action("Ayşe bütçe raporu hazırlasın")
    manager.add_action("Mehmet kalite raporu hazırlasın")
    assert manager.complete_action("Ayşe rapor").ok


def test_cancel_action(manager):
    manager.start_meeting()
    manager.add_action("Ayşe sunumu yarın hazırlasın")
    assert manager.cancel_action("sunum").ok
    assert manager.meeting.action_items[0].status == ActionStatus.CANCELLED
    assert manager.all_open_actions() == []


def test_open_actions_listing(manager):
    manager.start_meeting()
    assert manager.open_actions().message == "Açık görev yok."
    manager.add_action("Ayşe sunumu yarın hazırlasın")
    assert "1) Ayşe Yılmaz: sunumu hazırlasın" in manager.open_actions().message


# ------------------------------------------------------------------ konuşmacı
def test_set_speaker_marks_present_and_uses_locative(manager):
    manager.start_meeting()
    result = manager.set_speaker("Ayşe'de")
    assert result.message == "Söz Ayşe Yılmaz'da."
    assert manager.meeting.find_participant("Ayşe Yılmaz").present
    assert manager.set_speaker("Ayşe").data["changed"] is False


def test_set_speaker_unknown_rejected(manager):
    manager.start_meeting()
    result = manager.set_speaker("Fatma")
    assert not result.ok and manager.meeting.active_speaker is None


def test_set_speaker_ambiguous_rejected(manager):
    manager.start_meeting()
    assert not manager.set_speaker("Yılmaz").ok


def test_speaker_durations(manager, clock):
    manager.start_meeting()
    manager.mark_present(["Kenan", "Ayşe", "Mehmet"])
    manager.set_speaker("Ayşe")
    clock.advance(minutes=4)
    manager.set_speaker("Mehmet")
    clock.advance(minutes=1)
    release = manager.release_speaker()
    assert "Mehmet Öztürk sözü bıraktı" in release.message
    stats = manager.speaker_stats()
    assert stats.data["stats"][0] == ("Ayşe Yılmaz", 240.0, 1)
    assert manager.silent_participants().data["silent"] == ["Kenan Çetin"]
    assert not manager.release_speaker().ok


def test_end_meeting_closes_speaker_turn(manager, clock, repo):
    manager.start_meeting()
    manager.set_speaker("Ayşe")
    clock.advance(minutes=2)
    manager.end_meeting()
    turn = repo.get(manager.meeting.id).speaker_turns[0]
    assert turn.duration_seconds == 120


# ------------------------------------------------------------------ oylama, otopark, not, geri al
def test_vote_results(manager):
    manager.start_meeting()
    manager.mark_present(["Kenan", "Ayşe", "Mehmet"])
    accepted = manager.add_vote("Bütçe artışı", 2, 1, 0)
    assert accepted.data["result"] == VoteResult.ACCEPTED and "kabul edildi" in accepted.message
    assert manager.add_vote("X", 1, 2).data["result"] == VoteResult.REJECTED
    assert manager.add_vote("Y", 1, 1, 1).data["result"] == VoteResult.TIE
    too_many = manager.add_vote("Z", 5, 0)
    assert "katılımcı sayısından" in too_many.message
    assert not manager.add_vote("Eksik", None, 2).ok
    assert len(manager.meeting.votes) == 4


def test_vote_topic_defaults_to_agenda_item(manager):
    manager.start_meeting()
    manager.add_vote("", 3, 0)
    assert manager.meeting.votes[0].topic == "Üretim planlaması"


def test_parking_and_notes(manager):
    manager.start_meeting()
    assert manager.add_parking("Ofis taşınması").ok
    assert manager.add_note("Tedarikçi ile görüşülecek").ok
    assert manager.meeting.parking_lot == ["Ofis taşınması"]
    assert manager.meeting.agenda[0].notes == ["Tedarikçi ile görüşülecek"]
    assert not manager.add_parking("  ").ok


def test_note_without_active_item_goes_to_meeting(manager, settings):
    settings.agenda_file.unlink()
    manager.start_meeting()
    manager.add_note("Genel not")
    assert manager.meeting.notes == ["Genel not"]


def test_undo_last_record(manager):
    manager.start_meeting()
    manager.add_decision("Yanlış anlaşılan karar")
    assert manager.undo_last().ok
    assert manager.meeting.decisions == []
    assert not manager.undo_last().ok
    manager.add_action("Ayşe sunumu yarın hazırlasın")
    manager.undo_last()
    assert manager.meeting.action_items == []
    manager.add_note("not")
    manager.undo_last()
    assert manager.meeting.agenda[0].notes == []
    manager.add_vote("v", 1, 0)
    manager.undo_last()
    assert manager.meeting.votes == []
    manager.add_parking("p")
    manager.undo_last()
    assert manager.meeting.parking_lot == []


# ------------------------------------------------------------------ süre (G-12, T-05)
def test_t05_warning_once_at_80_percent(manager, clock):
    manager.start_meeting()  # ilk madde 15 dakika -> uyarı 12. dakikada
    clock.advance(minutes=11, seconds=59)
    assert manager.check_timebox() is None
    clock.advance(seconds=1)
    warning = manager.check_timebox()
    assert warning is not None and "yaklaşık 3 dakika kaldı" in warning.message
    clock.advance(minutes=1)
    assert manager.check_timebox() is None
    clock.advance(minutes=10)
    assert manager.check_timebox() is None


def test_warning_not_repeated_after_reopening_item(manager, clock):
    manager.start_meeting()
    clock.advance(minutes=13)
    assert manager.check_timebox() is not None
    manager.next_item()
    manager.previous_item()
    clock.advance(minutes=5)
    assert manager.check_timebox() is None


def test_warning_persisted_across_restart(manager, repo, settings, clock):
    manager.start_meeting()
    clock.advance(minutes=13)
    manager.check_timebox()
    restarted = MeetingManager(repo, settings, clock=clock)
    restarted.recover_active_meeting()
    assert restarted.check_timebox() is None


def test_elapsed_time_accumulates_across_reopen(manager, clock):
    manager.start_meeting()
    clock.advance(minutes=6)
    manager.next_item()
    manager.previous_item()
    clock.advance(minutes=6)  # toplam 12 dk = %80
    assert manager.check_timebox() is not None


def test_late_check_reports_overrun(manager, clock):
    manager.start_meeting()
    clock.advance(minutes=20)
    assert "süresi doldu" in manager.check_timebox().message


def test_time_status(manager, clock):
    manager.start_meeting()
    clock.advance(minutes=5)
    status = manager.time_status()
    assert "5 dakika geçti, 10 dakika kaldı" in status.message
    clock.advance(minutes=15)
    assert "aştı" in manager.time_status().message


def test_no_timebox_without_meeting(manager):
    assert manager.check_timebox() is None


# ------------------------------------------------------------------ sonraki toplantı, kapanış
def test_next_meeting_date(manager):
    manager.start_meeting()
    assert manager.set_next_meeting("21 Ekim").data["date"] == date(2026, 10, 21)
    assert manager.meeting.next_meeting_date == date(2026, 10, 21)
    assert not manager.set_next_meeting("bugün").ok
    assert not manager.set_next_meeting("bilmiyorum").ok


def test_end_meeting_stores_closing_summary(manager, clock):
    manager.start_meeting()
    manager.mark_present(["Ayşe", "Mehmet"])
    manager.set_speaker("Ayşe")
    clock.advance(minutes=3)
    manager.add_decision("Plan onaylandı")
    manager.add_action("tedarikçiyle görüşülsün")
    result = manager.end_meeting()
    assert "1 karar ve 1 görev" in result.message
    assert "En uzun söz süresi: Ayşe Yılmaz" in result.message  # canlı düzeltme: söz süresi
    assert "Söz almayan katılımcılar: Mehmet Öztürk" in result.message
    assert "sorumlusu veya son tarihi eksik" in result.message
    assert manager.meeting.summary == result.data["summary"]
