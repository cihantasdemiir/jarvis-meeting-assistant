"""Faz 6 düzeltmesi: katılımcı tam ad eşleştirme ve belirsizlik.

  * Tek eşleşme → rehberdeki tam ad, VERİTABANINA YAZILMADAN ÖNCE çözülür.
  * Birden çok eşleşme → "İki Mehmet var: … Hangisini kastediyorsunuz?" sorulur; yanıt tam ad, soyad veya
    «birinci/ikinci» olabilir. Çözülene kadar yoklama, söz veya görev sorumlusu kaydı yapılmaz.
  * Toplantı listesinde olmayan ama rehberde (participants.json) bulunan kişi rehberdeki ad ve e-postayla eklenir.
  * Eski kayıtlar otomatik değiştirilmez.
Gerçek mikrofon/STT/Qwen ve gerçek e-posta kullanılmaz (dry-run).
"""
from __future__ import annotations

import email
import io
import json
from email import policy
from pathlib import Path

import pytest

from jarvis.assistant import JarvisAssistant
from jarvis.dialog_flow import _ordinal_choice, choice_question
from jarvis.mail.mailer import Mailer, collect_recipients
from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.models import ActionItem, Meeting, Participant
from jarvis.reports.minutes_service import MinutesService
from jarvis.voice.command_parser import CommandParser
from jarvis.voice.speech_engine import SpeechEngine

DIRECTORY = [
    {"name": "Kenan Çetin", "email": "kenan@example.com", "role": "Başkan"},
    {"name": "Mehmet Öztürk", "email": "mehmet.ozturk@example.com"},
    {"name": "Mehmet Yılmaz", "email": "mehmet.yilmaz@example.com"},
    {"name": "Ayşe Kaya", "email": "ayse@example.com"},
    {"name": "Ahmet Yılmaz", "email": "ahmet@example.com"},
]
QUESTION = "İki Mehmet var: Mehmet Öztürk ve Mehmet Yılmaz. Hangisini kastediyorsunuz?"


def write_directory(settings, people) -> None:
    Path(settings.participants_file).write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")


def stored(assistant) -> Meeting:
    return assistant.manager.repo.get(assistant.manager.meeting.id)


def roster(assistant) -> dict[str, bool]:
    return {p.name: p.present for p in stored(assistant).participants}


def dump(assistant) -> str:
    return json.dumps(stored(assistant).to_dict(), sort_keys=True, ensure_ascii=False, default=str)


@pytest.fixture
def two(assistant, settings):
    """Rehberde iki Mehmet ve aynı soyadlı iki Yılmaz."""
    write_directory(settings, DIRECTORY)
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


@pytest.fixture
def one(assistant):
    """conftest rehberi: tek Mehmet (Mehmet Öztürk)."""
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


# ====================================================================== soru ve sıra yanıtı
def test_question_wording():
    assert choice_question(["Mehmet Öztürk", "Mehmet Yılmaz"], "Mehmet").startswith(QUESTION)
    assert choice_question(["Ayşe Yılmaz", "Ahmet Yılmaz", "Can Yılmaz"], "Yılmaz'ı").startswith(
        "'Yılmaz' üç kişiyle eşleşiyor: Ayşe Yılmaz, Ahmet Yılmaz ve Can Yılmaz. Hangisini kastediyorsunuz?")
    assert "«birinci», «ikinci»" in choice_question(["A B", "A C"])


@pytest.mark.parametrize("answer,index", [
    ("Birinci", 0), ("birincisi", 0), ("ilki", 0), ("1", 0), ("İkinci.", 1), ("ikincisi", 1), ("2", 1),
    ("sonuncusu", 1), ("evet ikinci", 1), ("ikinci olan", 1),
])
def test_ordinal_answers(answer, index):
    assert _ordinal_choice(answer, 2) == index


@pytest.mark.parametrize("answer", ["üçüncü", "Mehmet", "ikinci toplantı", "birinci madde", ""])
def test_not_an_ordinal(answer):
    assert _ordinal_choice(answer, 2) is None


# ====================================================================== tek eşleşme → tam ad
def test_single_match_attendance_speaker_and_owner_use_full_name(one):
    r = one.handle_text("Jarvis Mehmet katıldı")
    assert r.ok and "Kaydedilen: Mehmet Öztürk." in r.message
    assert roster(one)["Mehmet Öztürk"] is True and "Mehmet" not in roster(one)
    one.handle_text("Jarvis söz Mehmet'te")
    assert stored(one).active_speaker == "Mehmet Öztürk"
    one.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    assert stored(one).action_items[-1].owner == "Mehmet Öztürk"


# ====================================================================== iki Mehmet
def test_two_mehmets_attendance_asks_and_records_nothing(two):
    before = dump(two)
    r = two.handle_text("Jarvis Mehmet katıldı")
    assert not r.ok and r.message.startswith(QUESTION)
    assert dump(two) == before


@pytest.mark.parametrize("answer,expected", [
    ("Mehmet Yılmaz", "Mehmet Yılmaz"), ("Yılmaz", "Mehmet Yılmaz"), ("Öztürk", "Mehmet Öztürk"),
    ("birinci", "Mehmet Öztürk"), ("İkinci", "Mehmet Yılmaz"), ("Jarvis, ikincisi", "Mehmet Yılmaz"),
])
def test_two_mehmets_answers(two, answer, expected):
    two.handle_text("Jarvis Mehmet katıldı")
    before = dump(two)
    r = two.handle_text(answer)
    assert f"onaylıyor musunuz: {expected}?" in r.message, r.message
    assert dump(two) == before  # onaydan önce kayıt yok
    two.handle_text("evet")
    present = {name for name, flag in roster(two).items() if flag}
    assert present == {expected}


def test_unrelated_answer_is_reasked_never_guessed(two):
    two.handle_text("Jarvis Mehmet katıldı")
    before = dump(two)
    r = two.handle_text("Jarvis, Ayşe")
    assert not r.ok and "Mehmet Öztürk ve Mehmet Yılmaz" in r.message
    assert dump(two) == before


def test_same_surname_different_people(two):
    before = dump(two)
    r = two.handle_text("Jarvis Yılmaz katıldı")
    assert not r.ok and r.message.startswith("'Yılmaz' iki kişiyle eşleşiyor: Mehmet Yılmaz ve Ahmet Yılmaz.")
    assert dump(two) == before
    r = two.handle_text("Ahmet")
    assert "onaylıyor musunuz: Ahmet Yılmaz?" in r.message
    two.handle_text("evet")
    assert roster(two)["Ahmet Yılmaz"] is True and roster(two)["Mehmet Yılmaz"] is False


def test_multi_name_attendance_waits_for_all_ambiguities(two):
    before = dump(two)
    r = two.handle_text("Jarvis Kenan, Ayşe ve Mehmet katıldı")
    assert not r.ok and r.message.startswith(QUESTION)
    assert dump(two) == before  # Kenan ve Ayşe de belirsizlik çözülene kadar işaretlenmez
    r = two.handle_text("Öztürk")
    assert "Kenan Çetin, Ayşe Kaya ve Mehmet Öztürk" in r.message
    assert dump(two) == before
    two.handle_text("evet")
    assert roster(two) == {"Kenan Çetin": True, "Mehmet Öztürk": True, "Mehmet Yılmaz": False, "Ayşe Kaya": True,
                           "Ahmet Yılmaz": False}


def test_two_ambiguous_names_asked_in_turn_without_duplicates(two):
    before = dump(two)
    r = two.handle_text("Jarvis Ayşe, Mehmet ve Yılmaz katıldı")
    assert r.message.startswith(QUESTION)
    r = two.handle_text("birinci")
    assert r.message.startswith("'Yılmaz' iki kişiyle eşleşiyor")
    assert dump(two) == before
    r = two.handle_text("Mehmet Öztürk")  # "Yılmaz" sorusuna Yılmaz olmayan yanıt: tahmin yok, yeniden sorulur
    assert "Mehmet Öztürk ve Mehmet Öztürk" not in r.message
    assert not any(roster(two).values())  # yoklamaya hâlâ kimse işlenmedi
    two.handle_text("iptal")
    two.handle_text("Jarvis Ayşe, Mehmet ve Yılmaz katıldı")
    two.handle_text("birinci")
    r = two.handle_text("Ahmet Yılmaz")
    assert "Ayşe Kaya, Mehmet Öztürk ve Ahmet Yılmaz?" in r.message
    two.handle_text("evet")
    participants = stored(two).participants
    assert [p.name for p in participants].count("Mehmet Öztürk") == 1 and len(participants) == 5
    assert roster(two)["Ahmet Yılmaz"] and roster(two)["Mehmet Öztürk"] and not roster(two)["Mehmet Yılmaz"]


def test_repeated_attendance_keeps_status_and_no_duplicate(two):
    two.handle_text("Jarvis Mehmet katıldı")
    two.handle_text("ikinci")
    two.handle_text("evet")
    r = two.handle_text("Jarvis Mehmet Yılmaz katıldı")
    assert "zaten" in r.message.lower()
    names = [p.name for p in stored(two).participants]
    assert names.count("Mehmet Yılmaz") == 1 and roster(two)["Mehmet Yılmaz"] is True


# ====================================================================== konuşmacı ve görev sorumlusu
def test_speaker_ambiguity(two):
    before = dump(two)
    r = two.handle_text("Jarvis söz Mehmet'te")
    assert r.message.startswith(QUESTION) and dump(two) == before
    r = two.handle_text("ikinci")
    assert "Mehmet Yılmaz'a söz vermemi" in r.message
    assert stored(two).active_speaker in (None, "")
    two.handle_text("evet")
    assert stored(two).active_speaker == "Mehmet Yılmaz"
    assert stored(two).speaker_turns[-1].speaker == "Mehmet Yılmaz"


def test_task_owner_ambiguity_asks_and_assigns_after_confirmation(two):
    r = two.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    assert r.ok and "Görev kaydedildi" in r.message and QUESTION in r.message
    assert stored(two).action_items[-1].owner == ""  # belirsizken sorumlu yazılmaz
    r = two.handle_text("Yılmaz")
    assert "sorumlusu Mehmet Yılmaz olarak kaydedilsin mi" in r.message
    assert stored(two).action_items[-1].owner == ""
    two.handle_text("evet")
    assert stored(two).action_items[-1].owner == "Mehmet Yılmaz"


def test_task_owner_question_cancelled_leaves_owner_empty(two):
    two.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    two.handle_text("birinci")
    two.handle_text("hayır")
    assert stored(two).action_items[-1].owner == ""


def test_task_owner_question_ignored_if_task_list_changed(two):
    two.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    first_id = stored(two).action_items[-1].id
    question = two.dialog.question
    two.manager.add_action("Ayşe sunumu hazırlasın")  # bu arada başka görev eklendi (ör. ikinci komut)
    two.dialog.question = question
    r = two.handle_text("birinci")
    assert not r.ok and "sorumlu atanmadı" in r.message
    owners = {a.id: a.owner for a in stored(two).action_items}
    assert owners[first_id] == ""


def test_explicit_owner_command_still_resolves_full_name(two):
    two.handle_text("Jarvis görev: Ayşe sunumu hazırlasın")
    two.handle_text("Jarvis sorumlu: Öztürk")
    two.handle_text("evet")
    assert stored(two).action_items[-1].owner in ("Ayşe Kaya", "Mehmet Öztürk")
    assert "Öztürk" != stored(two).action_items[-1].owner


# ====================================================================== rehber / yeni kişi
def test_directory_person_missing_from_meeting_list_is_added_with_directory_data(assistant, settings):
    write_directory(settings, [DIRECTORY[0]])  # toplantı açılırken rehberde yalnızca Kenan var
    assistant.handle_text("Jarvis toplantıyı başlat")
    write_directory(settings, [DIRECTORY[0], DIRECTORY[1]])  # rehber sonradan güncellendi
    r = assistant.handle_text("Jarvis Mehmet katıldı")
    assert r.ok and "Mehmet Öztürk" in r.message
    person = next(p for p in stored(assistant).participants if p.name == "Mehmet Öztürk")
    assert person.email == "mehmet.ozturk@example.com" and person.present
    assert "Mehmet" not in roster(assistant)
    assistant.handle_text("Jarvis söz Mehmet'te")
    assert stored(assistant).active_speaker == "Mehmet Öztürk"


def test_new_person_still_added_without_invented_surname(one):
    r = one.handle_text("Jarvis Selin katıldı")
    assert r.ok and "Selin" in roster(one) and roster(one)["Selin"] is True
    assert "Soyadı uydurulmadı" in r.message and "Selin olarak kaydettiğin" in r.message
    assert not any(n.startswith("Selin ") for n in roster(one))


def test_lookalike_name_does_not_create_second_record(one):
    r = one.handle_text("Jarvis Mehmet Öztürkoğlu katıldı")
    assert "Mehmet Öztürkoğlu" not in roster(one)
    assert [n for n in roster(one) if n.startswith("Mehmet")] == ["Mehmet Öztürk"]
    assert "Mehmet Öztürk" in r.message  # hangi kayıtlı kişiyle eşleştiği açıkça söylenir


def test_recovered_meeting_resolves_against_directory(settings, repo, clock):
    write_directory(settings, [DIRECTORY[0]])
    first = MeetingManager(repo, settings, clock=clock)
    first.start_meeting("Kurtarma")
    write_directory(settings, DIRECTORY)
    second = MeetingManager(repo, settings, clock=clock)  # program yeniden açıldı
    assert second.recover_active_meeting() is not None
    assistant = JarvisAssistant(second, CommandParser(settings.wake_word, settings.wake_word_aliases),
                                SpeechEngine(enabled=False, output=io.StringIO()),
                                minutes=MinutesService(repo, settings, mailer=Mailer(settings, dry_run=True),
                                                       clock=clock))
    r = assistant.handle_text("Jarvis Mehmet katıldı")
    assert r.message.startswith(QUESTION)
    assert "Mehmet" not in roster(assistant)
    assistant.handle_text("Öztürk")
    assistant.handle_text("evet")
    person = next(p for p in stored(assistant).participants if p.name == "Mehmet Öztürk")
    assert person.present and person.email == "mehmet.ozturk@example.com"


# ====================================================================== e-posta, PDF, Excel
def test_recipients_use_directory_email(two):
    two.handle_text("Jarvis Mehmet katıldı")
    two.handle_text("ikinci")
    two.handle_text("evet")
    recipients = collect_recipients(stored(two), two.minutes.settings)
    by_name = {r.name: r.email for r in recipients.recipients}
    assert by_name["Mehmet Yılmaz"] == "mehmet.yilmaz@example.com"  # aynı adlı diğer kişinin adresi değil
    if two.minutes.settings.mail_recipients_mode == "present":
        assert "mehmet.ozturk@example.com" not in recipients.emails
    emails = {p.name: p.email for p in stored(two).participants}
    assert emails["Mehmet Yılmaz"] == "mehmet.yilmaz@example.com"
    assert emails["Mehmet Öztürk"] == "mehmet.ozturk@example.com"


def _finished(two):
    two.handle_text("Jarvis Kenan ve Mehmet katıldı")
    two.handle_text("ikinci")
    two.handle_text("evet")
    two.handle_text("Jarvis söz Mehmet Yılmaz'da")
    two.handle_text("evet")
    two.handle_text("Jarvis görev: Mehmet raporu cumaya kadar hazırlasın")
    two.handle_text("ikinci")
    two.handle_text("evet")
    two.handle_text("Jarvis karar: Plan onaylandı")
    meeting = stored(two)
    assert meeting.action_items[-1].owner == "Mehmet Yılmaz"
    return meeting


def test_full_name_in_pdf(two):
    pypdf = pytest.importorskip("pypdf")
    meeting = _finished(two)
    result = two.minutes.create_pdf(meeting)
    assert result.ok
    text = "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(result.data["path"])).pages)
    assert "Mehmet Yılmaz" in text


def test_full_name_in_html_email_dry_run(two):
    meeting = _finished(two)
    two.handle_text("Jarvis toplantıyı bitir")
    result = two.minutes.send_minutes(stored(two))
    assert result.ok and result.data["mode"] == "dry-run"
    saved = email.message_from_bytes(Path(result.data["eml"]).read_bytes(), policy=policy.default)
    html = saved.get_body(preferencelist=("html",)).get_content()
    plain = saved.get_body(preferencelist=("plain",)).get_content()
    assert "Mehmet Yılmaz" in html and "Mehmet Yılmaz" in plain
    assert meeting.id == stored(two).id


def test_full_name_in_excel(two):
    openpyxl = pytest.importorskip("openpyxl")
    meeting = _finished(two)
    result = two.minutes.export_tasks(meeting)
    assert result.ok
    sheet = openpyxl.load_workbook(result.data["path"])["Görevler"]
    header = [c.value for c in sheet[1]]
    owners = [row[header.index("Sorumlu")].value for row in sheet.iter_rows(min_row=2)]
    assert owners == ["Mehmet Yılmaz"]


# ====================================================================== eski kayıtlar
def test_old_records_are_not_migrated(settings, repo, clock):
    old = Meeting(id="eski-1", title="Eski toplantı", started_at=clock(), ended_at=clock(),
                  participants=[Participant(name="Mehmet", email="", present=True)])
    old.action_items.append(ActionItem(id="g-1", text="raporu hazırlasın", owner="Mehmet", meeting_id="eski-1"))
    repo.save(old)
    raw_before = json.dumps(repo.get("eski-1").to_dict(), sort_keys=True, ensure_ascii=False, default=str)
    clock.advance(days=7)
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting("Yeni")
    manager.mark_present(["Mehmet"])
    manager.end_meeting()
    raw_after = json.dumps(repo.get("eski-1").to_dict(), sort_keys=True, ensure_ascii=False, default=str)
    assert raw_after == raw_before  # "Mehmet" → "Mehmet Öztürk" geriye dönük değiştirilmez
    assert repo.get("eski-1").action_items[0].owner == "Mehmet"
