"""Faz 5c.2 katılımcı düzeltmesi: yoklama düzeltmesi (A), listeden çıkarma (B), ad düzeltme (C), katılımcı listesi
ve her yeni toplantıda ayrı yoklama. conftest rehberi: Kenan Çetin, Ayşe Yılmaz, Mehmet Öztürk, Ahmet Yılmaz.
Gerçek mikrofon/STT/Qwen kullanılmaz.
"""
from __future__ import annotations

import json

import pytest

from jarvis.mail.mailer import collect_recipients
from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.models import ActionItem, Meeting, Participant
from jarvis.meeting.storage import StorageError
from jarvis.voice.command_parser import CommandParser, IntentType
from jarvis.voice.unaddressed import classify_unaddressed

PARSER = CommandParser("jarvis", ["carvis"])


@pytest.fixture
def a(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis katılımcılar: Kenan, Ayşe, Ahmet")
    return assistant


def snap(assistant) -> tuple[str, str]:
    meeting = assistant.manager.meeting
    stored = assistant.manager.repo.get(meeting.id)
    dump = lambda m: json.dumps(m.to_dict(), sort_keys=True, ensure_ascii=False, default=str)  # noqa: E731
    return dump(meeting), dump(stored)


def roster(assistant) -> dict[str, bool]:
    return {p.name: p.present for p in assistant.manager.repo.get(assistant.manager.meeting.id).participants}


def add_guest(assistant, name: str) -> None:
    r = assistant.handle_text(f"Jarvis {name} katıldı")
    if assistant.pending is not None:  # çok kelimeli liste dışı ad: onay ister
        assistant.handle_text("evet")
    assert name in roster(assistant), r.message


# ====================================================================== ayrıştırıcı
@pytest.mark.parametrize("text,intent,params", [
    ("Jarvis, Ahmet'i yoklamadan çıkar.", IntentType.MARK_ABSENT, {"name": "Ahmet'i"}),
    ("Jarvis, Ayşe yanlışlıkla katıldı görünüyor.", IntentType.MARK_ABSENT, {"name": "Ayşe"}),
    ("Jarvis, Ayşe yanlışlıkla katıldı", IntentType.MARK_ABSENT, {"name": "Ayşe"}),
    ("Jarvis, Ayşe'nin yoklamasını geri al", IntentType.MARK_ABSENT, {"name": "Ayşe'nin"}),
    ("Jarvis, Recep'i bu toplantıdan kaldır.", IntentType.REMOVE_PARTICIPANT, {"name": "Recep'i"}),
    ("Jarvis, yanlış eklenen Mehmet Ayşe kaydını kaldır.", IntentType.REMOVE_PARTICIPANT, {"name": "Mehmet Ayşe"}),
    ("Jarvis, Ahmet olarak kaydettiğin kişinin adı aslında Ahmet Yılmaz.", IntentType.RENAME_PARTICIPANT,
     {"name": "Ahmet", "new_name": "Ahmet Yılmaz"}),
    ("Jarvis, Recep'in adını Recai Kaya olarak düzelt", IntentType.RENAME_PARTICIPANT,
     {"name": "Recep'in", "new_name": "Recai Kaya"}),
    ("Jarvis, katılımcı listesini oku.", IntentType.LIST_PARTICIPANTS, {}),
])
def test_parser(text, intent, params):
    parsed = PARSER.parse(text)
    assert parsed.type == intent
    assert {k: v for k, v in parsed.params.items() if k != "command"} == params


@pytest.mark.parametrize("text,intent", [
    ("Jarvis, Ayşe ve Mehmet katıldı", IntentType.MARK_PRESENT_MANY),
    ("Jarvis, Ayşe'nin yoklamasını al", IntentType.MARK_PRESENT),
    ("Jarvis, kim katılmadı", IntentType.ATTENDANCE),
    ("Jarvis, katılımcılar: Kenan, Ayşe", IntentType.MARK_PRESENT_MANY),
    ("Jarvis, en son eklediğin kaydı kaldır", IntentType.UNKNOWN),
])
def test_existing_commands_unchanged(text, intent):
    assert PARSER.parse(text).type == intent


# ====================================================================== A. yoklama düzeltmesi
def test_mark_absent_needs_confirmation_and_keeps_person(a):
    before = snap(a)
    r = a.handle_text("Jarvis, Ahmet'i yoklamadan çıkar.")
    assert "Ahmet Yılmaz yoklamadan çıkarılacak" in r.message and a.pending is not None
    assert snap(a) == before  # onaydan önce RAM ve SQLite değişmez
    r = a.handle_text("evet")
    assert r.ok and roster(a)["Ahmet Yılmaz"] is False  # listede kalır, katılmadı olur
    assert "Ahmet Yılmaz" in roster(a)


def test_mark_absent_rejected(a):
    before = snap(a)
    a.handle_text("Jarvis, Ayşe yanlışlıkla katıldı görünüyor.")
    a.handle_text("hayır")
    assert snap(a) == before


def test_mark_absent_closes_active_turn_and_keeps_records(a):
    a.handle_text("Jarvis söz Ayşe'de")
    a.handle_text("Bütçe tablosu hazır.")
    r = a.handle_text("Jarvis, Ayşe yanlışlıkla katıldı")
    assert "Söz şu anda onda" in r.message and "korunur" in r.message
    a.handle_text("evet")
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert stored.active_speaker is None and stored.speaker_turns[-1].ended_at is not None
    assert stored.transcript[-1].speaker == "Ayşe Yılmaz"  # konuşma kaydı silinmez/taşınmaz


def test_mark_absent_undo(a):
    a.handle_text("Jarvis, Ahmet'i yoklamadan çıkar")
    a.handle_text("evet")
    r = a.handle_text("Jarvis, geri al")
    assert "yeniden katıldı" in r.message and roster(a)["Ahmet Yılmaz"] is True


def test_ambiguous_name_asks_and_never_guesses(a):
    before = snap(a)
    r = a.handle_text("Jarvis, Yılmaz'ı yoklamadan çıkar")
    assert r.message.startswith("'Yılmaz' iki kişiyle eşleşiyor: Ayşe Yılmaz ve Ahmet Yılmaz.") and a.pending is None
    assert snap(a) == before
    r = a.handle_text("Ahmet Yılmaz")
    assert "Ahmet Yılmaz yoklamadan çıkarılacak" in r.message
    assert snap(a) == before
    a.handle_text("evet")
    assert roster(a) == {"Kenan Çetin": True, "Ayşe Yılmaz": True, "Mehmet Öztürk": False, "Ahmet Yılmaz": False}


def test_unknown_name_changes_nothing(a):
    before = snap(a)
    r = a.handle_text("Jarvis, Zeynep'i yoklamadan çıkar")
    assert r.ok is False and "listesinde yok" in r.message and snap(a) == before


def test_already_absent(a):
    r = a.handle_text("Jarvis, Mehmet'i yoklamadan çıkar")
    assert r.ok is False and "zaten katılmadı" in r.message and a.pending is None


# ====================================================================== B. listeden çıkarma
def test_remove_guest_without_relations(a, minutes):
    add_guest(a, "Recep")
    before = snap(a)
    r = a.handle_text("Jarvis, Recep'i bu toplantıdan kaldır.")
    assert "katılımcı listesinden çıkarılacak" in r.message and snap(a) == before
    a.handle_text("evet")
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert "Recep" not in [p.name for p in stored.participants]
    assert [(p.name, p.removed_reason) for p in stored.removed_participants] == [("Recep", "yanlış kayıt")]
    assert stored.removed_participants[0].removed_at is not None  # silinmedi, izlenebilir
    text = _pdf_text(minutes.create_pdf(a.manager.meeting).data["path"])
    assert "listeden çıkarılan kayıtlar: Recep (yanlış kayıt)" in text.replace("\n", " ")


def test_removed_participant_gets_no_minutes_email(a, settings):
    a.manager.meeting.participants.append(Participant("Yanlış Kişi", "yanlis@example.com", added_during_meeting=True,
                                                      present=True))
    a.manager.save()
    a.handle_text("Jarvis, yanlış eklenen Yanlış Kişi kaydını kaldır")
    a.handle_text("evet")
    emails = [r.email for r in collect_recipients(a.manager.meeting, settings).recipients]
    assert "yanlis@example.com" not in emails


def test_remove_undo_restores_position(a):
    add_guest(a, "Recep")
    order = list(roster(a))
    a.handle_text("Jarvis, Recep'i bu toplantıdan kaldır")
    a.handle_text("evet")
    a.handle_text("Jarvis, geri al")
    assert list(roster(a)) == order
    assert a.manager.repo.get(a.manager.meeting.id).removed_participants == []


@pytest.mark.parametrize("setup,relation", [
    (["Jarvis görev: Recep raporu cumaya kadar hazırlasın"], "görev"),
    (["Jarvis söz Recep'te", "Jarvis sözü bırak"], "söz kaydı"),
    (["Jarvis söz Recep'te"], "şu anda söz onda"),
])
def test_remove_refused_when_related_records_exist(a, setup, relation):
    add_guest(a, "Recep")
    for line in setup:
        a.handle_text(line)
    before = snap(a)
    r = a.handle_text("Jarvis, Recep'i bu toplantıdan kaldır")
    assert r.ok is False and relation in r.message and "sessizce silinmez" in r.message
    assert a.pending is None and snap(a) == before


def test_remove_refused_after_minutes_emailed(a):
    a.manager.meeting.email_status = {"sent_to": ["ayse@example.com"]}
    a.manager.save()
    r = a.handle_text("Jarvis, Ayşe'yi bu toplantıdan kaldır")
    assert r.ok is False and "e-postası gönderilmiş" in r.message


def test_remove_directory_person_keeps_directory_file(a, settings):
    before_file = settings.participants_file.read_text(encoding="utf-8")
    r = a.handle_text("Jarvis, Mehmet'i bu toplantıdan kaldır")
    assert "rehberde kalır" in r.message
    a.handle_text("evet")
    assert "Mehmet Öztürk" not in roster(a)
    assert settings.participants_file.read_text(encoding="utf-8") == before_file


# ====================================================================== C. ad düzeltme
def test_rename_guest_moves_this_meetings_relations(a, minutes):
    add_guest(a, "Recep")
    a.handle_text("Jarvis söz Recep'te")
    a.handle_text("Planı ben hazırladım.")
    a.handle_text("Jarvis görev: Recep raporu cumaya kadar hazırlasın")
    before = snap(a)
    r = a.handle_text("Jarvis, Recep olarak kaydettiğin kişinin adı aslında Recai Kaya")
    assert "'Recai Kaya' olarak düzeltilecek" in r.message and snap(a) == before
    a.handle_text("evet")
    stored = a.manager.repo.get(a.manager.meeting.id)
    person = next(p for p in stored.participants if p.name == "Recai Kaya")
    assert person.former_name == "Recep" and person.present
    assert stored.active_speaker == "Recai Kaya" and stored.speaker_turns[-1].speaker == "Recai Kaya"
    assert stored.transcript[-1].speaker == "Recai Kaya" and stored.action_items[-1].owner == "Recai Kaya"
    assert stored.transcript[-1].text == "Planı ben hazırladım."  # konuşmanın kendisi değişmez
    text = _pdf_text(minutes.create_pdf(a.manager.meeting).data["path"]).replace("\n", " ")
    assert "Recai Kaya (ilk kayıt: Recep)" in text


def test_rename_undo(a):
    add_guest(a, "Recep")
    a.handle_text("Jarvis görev: Recep raporu cumaya kadar hazırlasın")
    a.handle_text("Jarvis, Recep olarak kaydettiğin kişinin adı aslında Recai Kaya")
    a.handle_text("evet")
    a.handle_text("Jarvis, geri al")
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert "Recep" in roster(a) and stored.action_items[-1].owner == "Recep"


def test_merge_wrong_guest_into_directory_person(a):
    add_guest(a, "Kemal Soylu")
    a.handle_text("Jarvis söz Kemal Soylu'da")
    a.handle_text("Kalite verileri geldi.")
    r = a.handle_text("Jarvis, Kemal Soylu olarak kaydettiğin kişinin adı aslında Mehmet Öztürk")
    assert "Mehmet Öztürk ile birleştirilecek" in r.message
    a.handle_text("evet")
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert roster(a)["Mehmet Öztürk"] is True and "Kemal Soylu" not in roster(a)
    assert stored.removed_participants[0].merged_into == "Mehmet Öztürk"
    assert stored.transcript[-1].speaker == "Mehmet Öztürk" and stored.active_speaker == "Mehmet Öztürk"
    mehmet = next(p for p in stored.participants if p.name == "Mehmet Öztürk")
    assert mehmet.email == "mehmet@example.com"  # rehberdeki e-posta korunur
    a.handle_text("Jarvis, geri al")
    restored = a.manager.repo.get(a.manager.meeting.id)
    assert roster(a)["Mehmet Öztürk"] is False and roster(a)["Kemal Soylu"] is True
    assert restored.transcript[-1].speaker == "Kemal Soylu"


def test_rename_directory_person_refused(a):
    before = snap(a)
    r = a.handle_text("Jarvis, Kenan'ın adını Kenan Kaya olarak düzelt")
    assert r.ok is False and "rehberdeki" in r.message and snap(a) == before


def test_rename_target_ambiguous_asks(a):
    add_guest(a, "Recep")
    r = a.handle_text("Jarvis, Recep olarak kaydettiğin kişinin adı aslında Yılmaz")
    assert r.message.startswith("'Yılmaz' iki kişiyle eşleşiyor: Ayşe Yılmaz ve Ahmet Yılmaz.")
    r = a.handle_text("Ahmet Yılmaz")
    assert "Ahmet Yılmaz ile birleştirilecek" in r.message


def test_rename_to_non_name_refused(a):
    add_guest(a, "Recep")
    r = a.handle_text("Jarvis, Recep olarak kaydettiğin kişinin adı aslında yok")
    assert r.ok is False and a.pending is None


# ====================================================================== güvenilirlik
def test_confirmation_revalidates_changed_state(a):
    add_guest(a, "Recep")
    a.handle_text("Jarvis, Recep'i bu toplantıdan kaldır")
    a.manager.meeting.action_items.append(ActionItem(text="x", owner="Recep"))  # onay beklerken ilişki oluştu
    r = a.handle_text("evet")
    assert r.ok is False and "İşlem yapılmadı" in r.message
    assert "Recep" in [p.name for p in a.manager.meeting.participants]


def test_storage_failure_rolls_back(a, monkeypatch):
    add_guest(a, "Recep")
    a.handle_text("Jarvis, Recep'i bu toplantıdan kaldır")

    def broken(meeting):
        raise StorageError("disk dolu")
    monkeypatch.setattr(a.manager.repo, "save", broken)
    r = a.handle_text("evet")
    assert r.ok is False and "uygulanmadı" in r.message
    assert "Recep" in [p.name for p in a.manager.meeting.participants]
    assert a.manager.meeting.removed_participants == []


def test_unaddressed_correction_is_never_executed(a):
    result = classify_unaddressed(PARSER, "Ahmet'i yoklamadan çıkar")
    assert result.command_like and result.intent is None
    r = a.handle_text("Ahmet'i yoklamadan çıkar")
    assert r.is_command is False and a.pending is None and roster(a)["Ahmet Yılmaz"] is True


def test_list_participants(a):
    add_guest(a, "Recep")
    a.handle_text("Jarvis, Ahmet'i yoklamadan çıkar")
    a.handle_text("evet")
    r = a.handle_text("Jarvis, katılımcı listesini oku")
    assert r.intent == IntentType.LIST_PARTICIPANTS
    assert "Katıldı (3): Kenan Çetin, Ayşe Yılmaz, Recep" in r.message
    assert "Katılmadı (2): Mehmet Öztürk, Ahmet Yılmaz" in r.message and "eklenenler: Recep" in r.message


def test_participant_roundtrip_and_backward_compat():
    old = Participant.from_dict({"name": "A", "email": "", "role": "", "present": True})
    assert old.to_dict() == {"name": "A", "email": "", "role": "", "present": True, "added_during_meeting": False}
    meeting = Meeting(title="T", removed_participants=[Participant("B", removed_reason="yanlış kayıt",
                                                                   merged_into="A")])
    again = Meeting.from_dict(json.loads(json.dumps(meeting.to_dict(), default=str)))
    assert again.removed_participants[0].merged_into == "A"
    assert "removed_participants" not in Meeting(title="T").to_dict()


# ====================================================================== 3. her toplantıda ayrı yoklama
def test_new_meeting_starts_with_fresh_attendance(repo, settings, clock):
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting()
    manager.mark_present(["Kenan", "Ayşe", "Recep"])
    first_id = manager.meeting.id
    manager.end_meeting()
    clock.advance(days=7)
    manager.start_meeting()
    second = manager.meeting
    assert [p.name for p in second.participants] == ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk", "Ahmet Yılmaz"]
    assert not any(p.present for p in second.participants)  # önceki yoklama kopyalanmaz
    assert "Recep" not in [p.name for p in second.participants]  # misafir yalnızca kendi toplantısında
    first = repo.get(first_id)
    assert {p.name for p in first.participants if p.present} == {"Kenan Çetin", "Ayşe Yılmaz", "Recep"}


def test_corrections_never_touch_previous_meeting(assistant, repo, clock):
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis katılımcılar: Kenan, Ayşe")
    first_id = assistant.manager.meeting.id
    assistant.handle_text("Jarvis toplantıyı bitir")
    before = json.dumps(repo.get(first_id).to_dict(), sort_keys=True, default=str)
    clock.advance(days=7)
    assistant.handle_text("Jarvis toplantıyı başlat")
    assistant.handle_text("Jarvis katılımcılar: Kenan, Ayşe")
    assistant.handle_text("Jarvis, Ayşe yanlışlıkla katıldı")
    assistant.handle_text("evet")
    assert json.dumps(repo.get(first_id).to_dict(), sort_keys=True, default=str) == before


def test_recovery_is_not_a_new_meeting(repo, settings, clock):
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting()
    manager.mark_present(["Kenan"])
    meeting_id = manager.meeting.id
    restarted = MeetingManager(repo, settings, clock=clock)
    assert restarted.recover_active_meeting() is not None
    assert restarted.meeting.id == meeting_id and restarted.meeting.find_participant("Kenan Çetin").present
    refused = restarted.start_meeting()
    assert refused.ok is False and "Zaten açık bir toplantı var" in refused.message


def test_custom_participant_list_for_new_meeting(repo, settings, clock, tmp_path):
    team = tmp_path / "ekip_b.json"
    team.write_text(json.dumps([{"name": "Zeynep Kara", "email": "z@example.com"}]), encoding="utf-8")
    manager = MeetingManager(repo, settings.with_overrides(participants_file=team), clock=clock)
    manager.start_meeting()
    assert [p.name for p in manager.meeting.participants] == ["Zeynep Kara"]


def test_cli_accepts_participant_and_agenda_files():
    import main
    args = main.parse_args(["--text", "--participants", "a.json", "--agenda", "b.json"])
    assert args.participants == "a.json" and args.agenda == "b.json"


def _pdf_text(path: str) -> str:
    pypdf = pytest.importorskip("pypdf")
    return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(path).pages)
