"""Diyalog yanıtları transkripte karışmamalı.

Senaryo: söz Ayşe'de, "görev: Mehmet raporu haftaya kadar hazırlasın", rehberde iki Mehmet. Kişi seçimine ve onaya
verilen yanıtlar ("Mehmet Öztürk", "birinci") Ayşe'nin konuşması olarak Ek A'ya yazıldı.

Kök nedenler (kod incelemesiyle):
  1. Seçimden sonra soru kapanıp yerine onay açılır. Onay beklerken yalnızca salt "evet/hayır" diyaloğa aitti;
     seçimin tekrarı ("Mehmet Öztürk", "birinci") normal konuşma sayılıp transkripte yazılıyordu.
  2. Hitapsız yanıt penceresi (20 sn) soru OLUŞTURULDUĞU anda başlıyordu. Sesli girişte uzun soru seslendirilirken
     süre doluyor, kullanıcının yanıtı transkripte düşüyordu. Pencere artık seslendirme bittiğinde başlar.
Gerçek mikrofon/STT/Qwen kullanılmaz.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

DIRECTORY = [
    {"name": "Kenan Çetin", "email": "kenan@example.com"},
    {"name": "Ayşe Yılmaz", "email": "ayse@example.com"},
    {"name": "Mehmet Öztürk", "email": "mehmet.ozturk@example.com"},
    {"name": "Mehmet Yılmaz", "email": "mehmet.yilmaz@example.com"},
]
TASK = "Jarvis, görev: Mehmet raporu haftaya kadar hazırlasın"


class Tick:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def a(assistant, settings):
    Path(settings.participants_file).write_text(json.dumps(DIRECTORY, ensure_ascii=False), encoding="utf-8")
    assistant.dialog.clock = Tick()
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


def transcript(a) -> list[tuple[str, str]]:
    stored = a.manager.repo.get(a.manager.meeting.id)
    return [(e.speaker, e.text) for e in stored.transcript]


def owner(a) -> str:
    return a.manager.repo.get(a.manager.meeting.id).action_items[-1].owner


# ====================================================================== gerçek senaryo
@pytest.mark.parametrize("with_speaker", [True, False])
def test_live_scenario_dialog_answers_not_in_transcript(a, with_speaker):
    if with_speaker:
        a.handle_text("Jarvis, söz Ayşe'de")
    before = transcript(a)
    r = a.handle_text(TASK)
    assert "İki Mehmet var" in r.message
    r = a.handle_text("Mehmet Öztürk")
    assert "Mehmet Öztürk olarak kaydedilsin mi" in r.message
    r = a.handle_text("Mehmet Öztürk")  # kullanıcı duyulmadı sanıp tekrarladı
    assert r.is_command and "onayınızı bekliyor" in r.message
    r = a.handle_text("birinci")
    assert r.is_command and "Mehmet Öztürk seçildi" in r.message
    assert transcript(a) == before  # diyalog yanıtları transkript sayısını artırmaz
    assert owner(a) == ""  # onaydan önce kayıt yok
    a.handle_text("evet")
    assert owner(a) == "Mehmet Öztürk" and transcript(a) == before
    if with_speaker:
        assert a.manager.repo.get(a.manager.meeting.id).active_speaker == "Ayşe Yılmaz"
    a.handle_text("Raporu cuma sabahı paylaşırım.")  # diyalog bitti: normal konuşma kaydı devam eder
    expected_speaker = "Ayşe Yılmaz" if with_speaker else transcript(a)[-1][0]
    assert transcript(a)[-1] == (expected_speaker, "Raporu cuma sabahı paylaşırım.")


def test_changing_choice_while_confirmation_pending(a):
    a.handle_text(TASK)
    a.handle_text("birinci")
    r = a.handle_text("ikinci")
    assert "Mehmet Yılmaz olarak kaydedilsin mi" in r.message
    a.handle_text("evet")
    assert owner(a) == "Mehmet Yılmaz" and transcript(a) == []


def test_cancel_then_name_is_real_speech(a):
    a.handle_text("Jarvis, söz Ayşe'de")
    a.handle_text(TASK)
    a.handle_text("Mehmet Öztürk")
    a.handle_text("hayır")
    assert owner(a) == "" and transcript(a) == []
    a.handle_text("Mehmet Öztürk")  # artık bekleyen diyalog yok: gerçek konuşma
    assert transcript(a) == [("Ayşe Yılmaz", "Mehmet Öztürk")]


def test_name_without_dialog_is_recorded(a):
    a.handle_text("Jarvis, söz Ayşe'de")
    a.handle_text("Mehmet Öztürk")
    a.handle_text("birinci")
    assert transcript(a) == [("Ayşe Yılmaz", "Mehmet Öztürk"), ("Ayşe Yılmaz", "birinci")]


def test_unrelated_speech_during_confirmation_still_recorded(a):
    a.handle_text("Jarvis, söz Ayşe'de")
    a.handle_text(TASK)
    a.handle_text("Mehmet Öztürk")
    a.handle_text("Bu rapor müşteri için çok önemli.")
    a.handle_text("Kenan Çetin")  # seçimin adaylarından biri değil: konuşma
    assert transcript(a) == [("Ayşe Yılmaz", "Bu rapor müşteri için çok önemli."), ("Ayşe Yılmaz", "Kenan Çetin")]
    assert a.pending is not None  # onay beklemeye devam eder
    a.handle_text("evet")
    assert owner(a) == "Mehmet Öztürk"


def test_speaker_choice_repeat_not_recorded(a):
    r = a.handle_text("Jarvis, söz Mehmet'te")
    assert "İki Mehmet var" in r.message
    a.handle_text("ikinci")
    a.handle_text("ikinci")
    a.handle_text("Yılmaz")
    a.handle_text("evet")
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert stored.active_speaker == "Mehmet Yılmaz" and stored.transcript == []


def test_multi_step_attendance_answers_not_recorded(a):
    a.handle_text("Jarvis Kenan, Ayşe ve Mehmet katıldı")
    a.handle_text("Öztürk")
    a.handle_text("Mehmet Öztürk")  # onay beklerken tekrar
    a.handle_text("evet")
    stored = a.manager.repo.get(a.manager.meeting.id)
    assert stored.transcript == []
    assert {p.name for p in stored.participants if p.present} == {"Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk"}


def test_addressed_answers_still_work(a):
    a.handle_text(TASK)
    a.handle_text("Jarvis, Mehmet Öztürk")
    a.handle_text("Jarvis, evet")
    assert owner(a) == "Mehmet Öztürk" and transcript(a) == []


# ====================================================================== sesli giriş: pencere seslendirmeden sonra
def test_window_starts_after_spoken_reply(a):
    a.handle_text("Jarvis, söz Ayşe'de")
    a.handle_text(TASK)
    clock = a.dialog.clock
    clock.now += 25  # uzun soru 25 sn boyunca seslendirildi
    a.dialog.speech_ended()
    clock.now += 4  # kullanıcı soru bittikten 4 sn sonra yanıtladı
    r = a.handle_text("Mehmet Öztürk")
    assert "Mehmet Öztürk olarak kaydedilsin mi" in r.message and transcript(a) == []
    clock.now += 12
    a.dialog.speech_ended()
    clock.now += 3
    a.handle_text("evet")
    assert owner(a) == "Mehmet Öztürk" and transcript(a) == []


def test_without_speech_window_behaviour_unchanged(a):
    a.handle_text("Jarvis, söz Ayşe'de")
    a.handle_text(TASK)
    a.dialog.clock.now += 25  # seslendirme yok (klavye / --no-tts): eski pencere kuralı aynen geçerli
    a.handle_text("Mehmet Öztürk")
    assert transcript(a) == [("Ayşe Yılmaz", "Mehmet Öztürk")] and owner(a) == ""


def test_speech_engine_reports_end_to_dialog(a):
    assert a.speech.on_speech_end == a.dialog.speech_ended
