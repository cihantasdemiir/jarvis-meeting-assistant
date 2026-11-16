"""Faz 6: sonraki toplantı takvim daveti (.ics). Gerçek e-posta gönderilmez (dry-run / yerel sahte sunucu)."""
from __future__ import annotations

import email
import json
from datetime import date, datetime, timezone
from email import policy
from pathlib import Path

import pytest

from jarvis.mail.mailer import Mailer
from jarvis.reports.ics import InviteDetails, build_ics, escape_text, fold
from jarvis.reports.minutes_service import MinutesService
from jarvis.utils.dates import find_time
from jarvis.voice.command_parser import CommandParser, IntentType

PARSER = CommandParser("jarvis", ["carvis"])


def unfold(text: str) -> list[str]:
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")  # yalnızca CRLF
    return text.replace("\r\n ", "").split("\r\n")


def props(text: str) -> dict[str, str]:
    out = {}
    for line in unfold(text):
        if ":" in line:
            key, value = line.split(":", 1)
            out.setdefault(key, value)
    return out


@pytest.fixture
def a(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


def invite_text(assistant) -> str:
    return Path(assistant.manager.meeting.next_meeting_invite["path"]).read_bytes().decode("utf-8")


# ====================================================================== saat ayrıştırma
@pytest.mark.parametrize("text,expected", [
    ("21 Ekim saat 14.00", "14:00"), ("saat 14:30'da", "14:30"), ("saat 9 buçukta", "09:30"),
    ("öğleden sonra saat 2", "14:00"), ("akşam saat 7", "19:00"), ("saat on dört", "14:00"),
    ("21 ekim 14.00'te", "14:00"), ("sabah saat 9", "09:00"),
])
def test_time_parsing(text, expected):
    match = find_time(text)
    assert match is not None and not match.ambiguous and not match.error and match.hhmm == expected


@pytest.mark.parametrize("text", ["saat 2", "saat iki buçukta", "saat 5"])
def test_ambiguous_time(text):
    assert find_time(text).ambiguous


@pytest.mark.parametrize("text", ["saat 25.00", "saat 14.75", "saat 14.5"])
def test_invalid_time(text):
    assert find_time(text).error


@pytest.mark.parametrize("text", ["21 Ekim", "15.10.2026", "raporu cumaya kadar hazırla"])
def test_no_time(text):
    assert find_time(text) is None


# ====================================================================== komutlar
@pytest.mark.parametrize("text,intent", [
    ("Jarvis, sonraki toplantı 21 Ekim saat 14.00.", IntentType.NEXT_MEETING),
    ("Jarvis, sonraki toplantı için takvim daveti oluştur.", IntentType.CREATE_INVITE),
    ("Jarvis, takvim daveti oluştur", IntentType.CREATE_INVITE),
    ("Jarvis, toplantı davetini e-postaya ekle.", IntentType.ATTACH_INVITE),
])
def test_parser(text, intent):
    assert PARSER.parse(text).type == intent


def test_full_flow_message_and_file(a):
    r = a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00.")
    assert r.ok and "21 Ekim 2026 Çarşamba saat 14.00 olarak kaydedildi" in r.message
    assert "Takvim daveti oluşturuldu" in r.message
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert stored.next_meeting_date == date(2026, 10, 21) and stored.next_meeting_time == "14:00"
    p = props(invite_text(a))
    assert p["DTSTART;TZID=Europe/Istanbul"] == "20261021T140000"
    assert p["DTEND;TZID=Europe/Istanbul"] == "20261021T150000"  # varsayılan 60 dk
    assert p["TZID"] == "Europe/Istanbul" and p["TZOFFSETTO"] == "+0300"
    assert p["METHOD"] == "PUBLISH" and p["SEQUENCE"] == "0" and p["DTSTAMP"].endswith("Z")
    assert p["LOCATION"] == "Oda 1"


def test_missing_time_never_invented(a):
    r = a.handle_text("Jarvis, sonraki toplantı 21 Ekim")
    assert r.ok and "Saat belirtilmediği için takvim daveti oluşturulmadı" in r.message
    assert a.manager.meeting.next_meeting_invite == {} and a.manager.meeting.next_meeting_time == ""
    r = a.handle_text("Jarvis, takvim daveti oluştur")
    assert not r.ok and "saati belirtilmediği" in r.message


@pytest.mark.parametrize("command,needle", [
    ("Jarvis, sonraki toplantı 21 Ekim saat 2", "sabah mı öğleden sonra mı"),
    ("Jarvis, sonraki toplantı 21 Ekim saat 25.00", "geçerli bir saat değil"),
    ("Jarvis, sonraki toplantı 31 Şubat saat 14.00", "anlaşılamadı"),
    ("Jarvis, sonraki toplantı 10 Ekim 2026 saat 14.00", "bugünden sonra"),
    ("Jarvis, sonraki toplantı saat 14.00", "anlaşılamadı"),
])
def test_invalid_or_ambiguous_saves_nothing(a, command, needle):
    before = json.dumps(a.manager.repo.get(a.manager.meeting.id).to_dict(), sort_keys=True, default=str)
    r = a.handle_text(command)
    assert not r.ok and needle in r.message
    assert json.dumps(a.manager.repo.get(a.manager.meeting.id).to_dict(), sort_keys=True, default=str) == before


def test_repeated_invite_is_not_duplicated(a, settings):
    a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00")
    first = invite_text(a)
    r = a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00")
    assert "zaten güncel" in r.message and invite_text(a) == first
    a.handle_text("Jarvis, sonraki toplantı 22 Ekim saat 10.30")
    updated = props(invite_text(a))
    assert updated["UID"] == props(first)["UID"] and updated["SEQUENCE"] == "1"  # güncelleme, kopya değil
    assert updated["DTSTART;TZID=Europe/Istanbul"] == "20261022T103000"
    files = list((settings.output_dir / "davetler").glob("*.ics"))
    assert len(files) == 1


def test_date_change_without_time_detaches_old_invite(a):
    a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00")
    r = a.handle_text("Jarvis, sonraki toplantı 23 Ekim")
    assert "Önceki takvim daveti artık geçerli değil" in r.message
    assert a.minutes.invite_attachment(a.manager.meeting) is None


def test_turkish_and_special_characters_escaped():
    details = InviteDetails(uid="x@y", title="Üretim; Planlama, Şubat \\ Çalışma", day=date(2026, 10, 21),
                            hhmm="14:00", duration_min=45, location="İzmir Ofisi, Kat 3",
                            description="Satır 1\nSatır 2; ğüşıöç " + "uzun açıklama " * 10)
    text = build_ics(details, 0, stamp=datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc))
    for raw_line in text.split("\r\n"):
        assert len(raw_line.encode("utf-8")) <= 75  # katlama bayt sınırı
    p = props(text)
    assert p["SUMMARY"] == "Üretim\\; Planlama\\, Şubat \\\\ Çalışma"
    assert p["LOCATION"] == "İzmir Ofisi\\, Kat 3" and "Satır 1\\nSatır 2\\; ğüşıöç" in p["DESCRIPTION"]
    assert p["DTEND;TZID=Europe/Istanbul"] == "20261021T144500"
    text.encode("utf-8").decode("utf-8")  # geçerli UTF-8 (katlama karakter bölmedi)


def test_fold_keeps_multibyte_characters():
    line = "DESCRIPTION:" + "ğ" * 100
    assert all(len(part.encode("utf-8")) <= 75 for part in fold(line).split("\r\n"))
    assert fold(line).replace("\r\n ", "") == line
    assert escape_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


# ====================================================================== e-posta
def _ended_with_invite(a):
    a.handle_text("Jarvis katılımcılar: Kenan, Ayşe")
    a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00")
    a.handle_text("Jarvis toplantıyı bitir")
    return a.manager.meeting


def test_dry_run_eml_contains_pdf_and_ics(a):
    meeting = _ended_with_invite(a)
    result = a.minutes.send_minutes(meeting)
    assert result.ok and result.data["mode"] == "dry-run"
    saved = email.message_from_bytes(Path(result.data["eml"]).read_bytes(), policy=policy.default)
    attachments = {part.get_filename(): part for part in saved.iter_attachments()}
    assert any(name.endswith(".pdf") for name in attachments)
    ics = next(part for name, part in attachments.items() if name.endswith(".ics"))
    assert ics.get_content_type() == "text/calendar"
    assert "DTSTART;TZID=Europe/Istanbul:20261021T140000" in ics.get_payload(decode=True).decode("utf-8")
    body = saved.get_body(preferencelist=("plain",)).get_content()
    assert "Sonraki toplantı: 21 Ekim 2026 Çarşamba, saat 14.00" in body


def test_attach_command_does_not_send(a, monkeypatch):
    a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00")
    sent = []
    monkeypatch.setattr(a.minutes.mailer, "send", lambda *args, **kw: sent.append(args))
    r = a.handle_text("Jarvis, toplantı davetini e-postaya ekle")
    assert r.ok and "yalnızca 'Jarvis, tutanağı gönder' ile" in r.message and sent == []


def test_attach_setting_off(a, settings, repo, clock):
    off = MinutesService(repo, settings.with_overrides(mail_attach_ics=False), mailer=Mailer(settings, dry_run=True),
                         clock=clock)
    a.minutes = off
    meeting = _ended_with_invite(a)
    result = off.send_minutes(meeting)
    saved = email.message_from_bytes(Path(result.data["eml"]).read_bytes(), policy=policy.default)
    assert not any((p.get_filename() or "").endswith(".ics") for p in saved.iter_attachments())


def test_smtp_failure_keeps_ics_and_meeting(a, settings, repo, clock):
    failing = MinutesService(repo, settings.with_overrides(smtp_host="127.0.0.1", smtp_port=1,
                                                           smtp_from="j@example.net", smtp_timeout=2),
                             mailer=Mailer(settings.with_overrides(smtp_host="127.0.0.1", smtp_port=1,
                                                                   smtp_from="j@example.net", smtp_timeout=2),
                                           dry_run=False), clock=clock)
    a.minutes = failing
    meeting = _ended_with_invite(a)
    path = Path(meeting.next_meeting_invite["path"])
    result = failing.send_minutes(meeting)
    assert not result.ok and path.is_file()
    stored = repo.get(meeting.id)
    assert stored.next_meeting_time == "14:00" and stored.next_meeting_invite["path"] == str(path)


def test_invite_does_not_change_other_records(a):
    a.handle_text("Jarvis katılımcılar: Kenan, Ayşe")
    a.handle_text("Jarvis karar: Plan onaylandı")
    before = a.manager.repo.get(a.manager.meeting.id).to_dict()
    a.handle_text("Jarvis, sonraki toplantı 21 Ekim saat 14.00")
    after = a.manager.repo.get(a.manager.meeting.id).to_dict()
    for key in ("participants", "decisions", "action_items", "transcript", "agenda", "speaker_turns"):
        assert after[key] == before[key]
    assert set(after) - set(before) == {"next_meeting_time", "next_meeting_invite"}


def test_old_payload_without_time_loads():
    from jarvis.meeting.models import Meeting
    meeting = Meeting.from_dict({"id": "x", "title": "T", "next_meeting_date": "2026-10-21"})
    assert meeting.next_meeting_time == "" and meeting.next_meeting_invite == {}
    assert "next_meeting_time" not in meeting.to_dict()
