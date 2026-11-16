"""Faz 5c.1 kapanış: gerçek qwen3.5:4b ölçümünde kalan D33 (yarım katılım cümlesi) ve HR19 (geçmiş toplantı sorusu).

Değerlendirme setlerindeki cümleler burada kullanılmadı (gerçek çıktıların yeniden oynatıldığı son test hariç).
Sahte model gerçek Qwen doğruluğunu ölçmez. conftest katılımcıları: Kenan Çetin, Ayşe Yılmaz, Mehmet Öztürk,
Ahmet Yılmaz.
"""
from __future__ import annotations

from datetime import date

import pytest

from jarvis.llm import benchmark as bench
from jarvis.llm.analyzer import MeetingAnalyzer
from jarvis.llm.router import PAST_OR_CURRENT_MESSAGE, LlmCommandRouter
from jarvis.llm.services import LlmServices
from jarvis.llm.utterance_guard import dangling_fragment
from jarvis.voice.command_parser import CommandParser, IntentType
from tests.llm_fakes import FakeLlm

PARSER = CommandParser("jarvis", ["carvis"])
PEOPLE = ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk", "Ahmet Yılmaz"]
DAY = date(2026, 10, 14)


def model(intent, category="command", confidence=0.9, **arguments):
    return {"intent": intent, "arguments": arguments, "confidence": confidence, "category": category}


def route(answer, command):
    fake = FakeLlm(answer)
    return LlmCommandRouter(fake, "m", PARSER).route(command, PEOPLE, DAY), fake


@pytest.fixture
def started(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


def with_llm(assistant, answer):
    fake = FakeLlm(answer)
    assistant.llm = LlmServices(client=fake, model="m", router=LlmCommandRouter(fake, "m", assistant.parser),
                                analyzer=MeetingAnalyzer(fake, "m"))
    return fake


def attendance(assistant):
    meeting = assistant.manager.meeting
    stored = assistant.manager.repo.get(meeting.id)
    return (sorted((p.name, p.present) for p in meeting.participants),
            sorted((p.name, p.present) for p in stored.participants))


# ====================================================================== D33: yarım katılım cümlesi
@pytest.mark.parametrize("command,fragment", [
    ("Kenan ve Ayşe geldi, Ahmet de", "ahmet de"),
    ("Mehmet katıldı, bir de", "bir de"),
    ("Ayşe burada, Kenan ile", "kenan ile"),
    ("Mehmet raporu bitirdi, Ayşe de", "ayşe de"),
])
def test_dangling_fragment_detected(command, fragment):
    assert dangling_fragment(command) == fragment


@pytest.mark.parametrize("command", [
    "Kenan, Ayşe ve Ahmet de geldi",          # tamamlanmış liste
    "Kenan geldi, Ahmet de geldi",            # ikinci yan cümle de tamam
    "Kenan ve Ayşe geldi",
    "Ayşe de, Mehmet de geldi",
])
def test_complete_sentences_are_not_fragments(command):
    assert dangling_fragment(command) is None


@pytest.mark.parametrize("answer", [
    model("ATTENDANCE", names=["Kenan", "Ayşe", "Ahmet"]),   # D33'teki gerçek hata sınıfı: okuma çalıştı
    model("MARK_PRESENT_MANY", names=["Kenan", "Ayşe"]),      # ilk kısım işlenip kalan atlanırdı
])
def test_incomplete_attendance_asks_and_never_reaches_model(answer):
    decision, fake = route(answer, "Kenan ve Ayşe geldi, Ahmet de")
    assert decision.kind == "clarify" and decision.category == "incomplete" and decision.intent is None
    assert fake.calls == [] and "ahmet de" in decision.message


def test_incomplete_attendance_records_nobody_in_assistant(started):
    with_llm(started, model("MARK_PRESENT_MANY", names=["Kenan", "Ayşe"]))
    before = attendance(started)
    response = started.handle_text("Jarvis, Kenan ve Ayşe geldi, Ahmet de")
    assert response.ok is False and "Hiçbir kayıt yapılmadı" in response.message
    assert attendance(started) == before and started.pending is None


def test_incomplete_attendance_without_llm(started):
    before = attendance(started)
    response = started.handle_text("Jarvis, Kenan ve Ayşe geldi, Ahmet de")
    assert "yarım" in response.message and response.ok is False and attendance(started) == before


def test_attendance_report_is_never_a_read_query():
    decision, _ = route(model("ATTENDANCE", names=["Kenan", "Mehmet"]), "Kenan ile Mehmet az önce geldi")
    assert decision.kind == "confirm" and decision.intent.type == IntentType.MARK_PRESENT_MANY
    decision, _ = route(model("ATTENDANCE", "question"), "kimler geldi?")
    assert decision.kind == "execute" and decision.intent.type == IntentType.ATTENDANCE


def test_complete_attendance_commands_unchanged(started):
    started.handle_text("Jarvis, Kenan, Ayşe ve Ahmet de geldi")
    assert {p.name for p in started.manager.meeting.participants if p.present} == {
        "Kenan Çetin", "Ayşe Yılmaz", "Ahmet Yılmaz"}


# ====================================================================== HR19: geçmiş toplantı sorusu
@pytest.mark.parametrize("command", [
    "Geçen toplantıda neleri görüşmüştük?", "Önceki toplantımızda neler konuşuldu?",
    "Son toplantıyı bana hatırlatır mısın?", "Bir önceki toplantının kararlarını özetler misin?",
    "dünkü toplantıda ne karar vermiştik", "geçen ayki toplantıda hangi konular görüşüldü",
])
@pytest.mark.parametrize("answer", [model("NONE", "question"), model("NONE", "chat", 0.2), model("READ_AGENDA")])
def test_past_meeting_questions_read_previous_summary(command, answer):
    decision, _ = route(answer, command)
    assert decision.kind == "execute" and decision.intent.type == IntentType.SUMMARIZE_PREVIOUS


@pytest.mark.parametrize("command,answer,expected", [
    ("bugün neleri görüşeceğiz", model("NONE", "question"), IntentType.READ_AGENDA),
    ("bu toplantının konuları neler", model("READ_AGENDA"), IntentType.READ_AGENDA),
])
def test_current_meeting_questions_stay_current(command, answer, expected):
    decision, _ = route(answer, command)
    assert decision.kind == "execute" and decision.intent.type == expected


def test_previous_summary_chosen_for_current_meeting_asks():
    decision, _ = route(model("SUMMARIZE_PREVIOUS"), "bu toplantıda neler konuştuk")
    assert decision.kind == "clarify" and decision.message == PAST_OR_CURRENT_MESSAGE and decision.intent is None


def test_no_history_means_no_invented_content(started):
    with_llm(started, model("NONE", "question"))
    response = started.handle_text("Jarvis, geçen toplantıda neleri görüşmüştük?")
    assert response.intent == IntentType.SUMMARIZE_PREVIOUS
    assert "Kayıtlı önceki toplantı bulunamadı" in response.message


def test_past_meeting_question_with_write_verb_is_not_summary():
    decision, _ = route(model("NONE", "question"), "geçen toplantıdaki kararı bu toplantıya da ekle")
    assert not (decision.intent and decision.intent.type == IntentType.SUMMARIZE_PREVIOUS)


# ====================================================================== gerçek çıktıların yeniden oynatılması
def test_replay_reported_failures():
    """Kullanıcının bildirdiği sonuçlarla (yeni model ölçümü değildir)."""
    replies = {"D33": model("ATTENDANCE", names=["Kerem", "Nihat", "Selin"]),
               "HR19": model("NONE", "question")}
    for path in (bench.NATURAL_PATH, bench.HOLDOUT_PATH):
        data = bench.load_benchmark(path)
        cases = [c for c in data["router"] if c["id"] in replies]

        def responder(_s, user, _sc, cases=cases):
            return replies[next(c["id"] for c in cases if user["komut"] in c["text"])]
        results = bench.run_router_cases(cases, PARSER, LlmCommandRouter(FakeLlm(responder), "q", PARSER), "q",
                                         data["participants"], date.fromisoformat(data["meeting_date"]),
                                         data["agenda_item"])
        assert results and all(r.passed for r in results), [(r.case_id, r.detail) for r in results]
