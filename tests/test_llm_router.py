"""Güvenli komut yönlendirme: LLM yalnızca öneri yapar, kurallı sistem karar verir.

Bu testler sahte LLM kullanır; bir modelin gerçek doğruluğunu ölçmez. Ölçülen şey, model ne
döndürürse döndürsün uygulamanın güvenlik kurallarının çalışmasıdır.
"""
from __future__ import annotations

from datetime import date

import pytest

from jarvis.llm.client import LlmBadOutput, LlmUnavailable
from jarvis.llm.router import LlmCommandRouter, PendingConfirmation
from jarvis.llm.services import LlmServices
from jarvis.llm.analyzer import MeetingAnalyzer
from jarvis.voice.command_parser import CommandParser, IntentType
from tests.llm_fakes import FakeLlm, router_answer, timeout_responder

PARSER = CommandParser("jarvis", ["carvis"])
PEOPLE = ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk", "Mehmet Kaya", "Ahmet Yılmaz"]
DAY = date(2026, 10, 14)


def route(answer, command, people=PEOPLE):
    router = LlmCommandRouter(FakeLlm(answer), "qwen3:4b", PARSER)
    return router.route(command, people, DAY, "Üretim planlaması")


def test_read_only_command_executes_directly():
    d = route(router_answer("READ_AGENDA"), "gündemde neler var bugün söyler misin")
    assert d.kind == "execute" and d.intent.type == IntentType.READ_AGENDA and d.canonical == "gündemi oku"


def test_state_change_requires_confirmation_and_is_reparsed():
    d = route(router_answer("SET_SPEAKER", speaker="Ayşe"), "sözü Ayşe Hanım'a verelim")
    assert d.kind == "confirm" and d.canonical == "söz Ayşe Yılmaz'da"
    assert d.intent.type == IntentType.SET_SPEAKER and "evet" in d.message


@pytest.mark.parametrize("intent", ["END_MEETING", "SEND_MINUTES", "COMPLETE_ACTION", "CANCEL_ACTION",
                                    "UNDO_LAST", "EXIT"])
def test_important_operations_are_blocked(intent):
    d = route(router_answer(intent), "artık toplantıyı kapatalım bence")
    assert d.kind == "blocked" and d.intent is None and "açık komut" in d.message


def test_unknown_or_invented_intent_rejected_by_schema():
    d = route(router_answer("DELETE_DATABASE"), "veritabanını sil")
    assert d.kind == "error" and not d.json_valid and d.intent is None
    d = route({"intent": "READ_AGENDA", "arguments": {"sql": "DROP TABLE meetings"}, "confidence": 1},
              "gündemi oku lütfen")
    assert d.kind == "error" and "tanımsız alan" in d.rejected_reason


def test_confirm_cannot_come_from_llm():
    d = route(router_answer("CONFIRM"), "evet evet onaylıyorum")
    assert d.kind == "error"


def test_fabricated_speaker_not_in_utterance_is_rejected():
    d = route(router_answer("SET_SPEAKER", speaker="Kenan"), "sözü şimdi ona verelim")
    assert d.kind == "unknown" and "uydurma" in d.rejected_reason


def test_ambiguous_and_unknown_people_trigger_question():
    d = route(router_answer("SET_SPEAKER", speaker="Mehmet"), "sözü Mehmet'e verelim")
    assert d.kind == "clarify" and "Mehmet Öztürk" in d.message and "Mehmet Kaya" in d.message
    d = route(router_answer("SET_SPEAKER", speaker="Burak"), "sözü Burak'a verelim")
    assert d.kind == "clarify" and "listesinde yok" in d.message


def test_action_owner_and_date_are_validated():
    d = route(router_answer("ADD_ACTION", text="kalite raporunu hazırlasın", owner="Mehmet Öztürk",
                            date_text="cumaya kadar"),
              "Mehmet Öztürk kalite raporunu cumaya kadar hazırlasın bunu görev olarak kaydet")
    assert d.kind == "confirm"
    assert "sorumlu: Mehmet Öztürk" in d.canonical and "16 Ekim 2026" in d.message
    params_text = d.intent.params["text"]
    assert "cumaya kadar" in params_text


def test_action_with_invented_date_drops_date():
    d = route(router_answer("ADD_ACTION", text="raporu hazırlasın", owner="Ahmet", date_text="21 ekim"),
              "Ahmet raporu hazırlasın diye görev aç")
    assert d.kind == "confirm" and "Son tarih: belirtilmedi" in d.message and "tarih anlaşılamadı" in d.message


def test_action_with_ambiguous_owner_asks():
    d = route(router_answer("ADD_ACTION", text="raporu hazırlasın", owner="Mehmet"),
              "Mehmet raporu hazırlasın diye görev yaz")
    assert d.kind == "clarify"


def test_decision_text_must_come_from_user():
    d = route(router_answer("ADD_DECISION", text="maaşlara yüzde elli zam yapılacak"),
              "şunu karar olarak yaz: vardiya düzeni kasımda başlayacak")
    assert d.kind == "unknown" and "uydurma" in d.rejected_reason
    d = route(router_answer("ADD_DECISION", text="vardiya düzeni kasımda başlayacak"),
              "şunu karar olarak yaz: vardiya düzeni kasımda başlayacak")
    assert d.kind == "confirm" and d.canonical == "karar: vardiya düzeni kasımda başlayacak"


def test_mark_present_resolves_each_name():
    d = route(router_answer("MARK_PRESENT_MANY", names=["Kenan", "Mehmet Öztürk"]),
              "Kenan ve Mehmet Öztürk de aramızda")
    assert d.kind == "confirm" and d.canonical == "katılımcılar: Kenan Çetin, Mehmet Öztürk"


@pytest.mark.parametrize("failure", [LlmUnavailable("kapalı"), LlmBadOutput("bozuk"), timeout_responder()])
def test_llm_failure_falls_back_to_rule_message(failure):
    d = route(lambda *a: failure, "gündemde neler var")
    assert d.kind == "error" and "anlayamadım" in d.message and d.intent is None


def test_none_intent_is_unknown():
    assert route(router_answer("NONE"), "hava nasıl").kind == "unknown"


def test_pending_confirmation_expiry():
    pending = PendingConfirmation(intent=None, canonical="x", created_at=100.0, timeout_s=60)
    assert not pending.expired(now=150.0) and pending.expired(now=161.0)


# ====================================================================== asistan entegrasyonu
def make_services(fake: FakeLlm) -> LlmServices:
    return LlmServices(client=fake, model="qwen3:4b", router=LlmCommandRouter(fake, "qwen3:4b", PARSER),
                       analyzer=MeetingAnalyzer(), confirm_timeout_s=60)


@pytest.fixture
def llm_assistant(assistant):
    fake = FakeLlm(router_answer("NONE"))
    assistant.llm = make_services(fake)
    assistant.background_llm = False
    assistant.fake = fake
    assistant.handle_text("Jarvis, toplantıyı başlat")
    assistant.handle_text("Jarvis, katılımcılar: Kenan, Ayşe, Mehmet, Ahmet")
    return assistant


def test_normal_speech_never_reaches_llm(llm_assistant):
    for text in ["Bence toplantıyı bitirelim.", "Tüm görevleri silelim.", "Evet, katılıyorum.",
                 "Jarvis'in tutanağı göndermesi lazım."]:
        llm_assistant.handle_text(text)
    assert llm_assistant.fake.calls == []
    assert len(llm_assistant.manager.meeting.transcript) == 4


def test_rule_commands_do_not_use_llm(llm_assistant):
    llm_assistant.handle_text("Jarvis, gündemi oku")
    llm_assistant.handle_text("Jarvis, söz Ayşe'de")
    assert llm_assistant.fake.calls == []


def test_truncated_command_not_sent_to_llm(llm_assistant):
    response = llm_assistant.handle_text("Jarvis toplantıyı bit")
    assert "eksik" in response.message and llm_assistant.fake.calls == []
    assert llm_assistant.manager.has_active_meeting


def test_confirmation_flow_executes_only_after_yes(llm_assistant):
    llm_assistant.fake.responder = lambda *a: router_answer("SET_SPEAKER", speaker="Ayşe")
    response = llm_assistant.handle_text("Jarvis, sözü Ayşe Hanım'a verelim")
    assert "Jarvis, evet" in response.message
    assert llm_assistant.manager.meeting.active_speaker is None  # henüz uygulanmadı
    response = llm_assistant.handle_text("Jarvis, evet")
    assert response.ok and llm_assistant.manager.meeting.active_speaker == "Ayşe Yılmaz"
    assert not llm_assistant.handle_text("Jarvis, evet").ok  # ikinci "evet" bir şey yapmaz


def test_reject_and_other_command_drop_pending(llm_assistant):
    llm_assistant.fake.responder = lambda *a: router_answer("ADD_NOTE", text="tedarikçi fiyatları güncellenecek")
    llm_assistant.handle_text("Jarvis, bir kenara yaz tedarikçi fiyatları güncellenecek")
    assert llm_assistant.handle_text("Jarvis, hayır").ok
    assert not llm_assistant.handle_text("Jarvis, evet").ok
    llm_assistant.handle_text("Jarvis, bir kenara yaz tedarikçi fiyatları güncellenecek")
    llm_assistant.handle_text("Jarvis, gündemi oku")  # araya başka komut girdi
    assert not llm_assistant.handle_text("Jarvis, evet").ok
    meeting = llm_assistant.manager.meeting
    assert not meeting.notes and not any(item.notes for item in meeting.agenda)


def test_plain_yes_without_wake_word_is_transcript(llm_assistant):
    llm_assistant.fake.responder = lambda *a: router_answer("NEXT_ITEM")
    llm_assistant.handle_text("Jarvis, bir sonraki konuya geçebilir miyiz")
    before = llm_assistant.manager.meeting.current_item_index
    llm_assistant.handle_text("Evet, geçelim.")  # hitap yok: onay değildir
    assert llm_assistant.manager.meeting.current_item_index == before
    assert llm_assistant.pending is not None


def test_expired_confirmation_does_nothing(llm_assistant, monkeypatch):
    llm_assistant.fake.responder = lambda *a: router_answer("NEXT_ITEM")
    llm_assistant.handle_text("Jarvis, bir sonraki konuya geçebilir miyiz")
    before = llm_assistant.manager.meeting.current_item_index
    llm_assistant.pending.created_at -= 1000
    response = llm_assistant.handle_text("Jarvis, evet")
    assert "süresi doldu" in response.message
    assert llm_assistant.manager.meeting.current_item_index == before


def test_blocked_operation_explains_explicit_command(llm_assistant):
    llm_assistant.fake.responder = lambda *a: router_answer("END_MEETING")
    response = llm_assistant.handle_text("Jarvis, artık kapatalım bence")
    assert "Jarvis, toplantıyı bitir" in response.message
    assert llm_assistant.manager.has_active_meeting
    assert llm_assistant.pending is None


def test_llm_down_keeps_rule_system_working(llm_assistant):
    llm_assistant.fake.responder = lambda *a: LlmUnavailable("sunucu kapalı")
    response = llm_assistant.handle_text("Jarvis, gündemde neler var acaba")
    assert "anlayamadım" in response.message
    assert llm_assistant.handle_text("Jarvis, gündemi oku").ok


def test_unaddressed_text_mode_does_not_route(assistant):
    """TEXT_MODE_REQUIRE_WAKE=false olsa bile hitapsız metin LLM'ye gitmez."""
    fake = FakeLlm(router_answer("READ_AGENDA"))
    assistant.llm = make_services(fake)
    assistant.require_wake = False
    assistant.handle_text("gündemde neler var acaba")
    assert fake.calls == []


def test_llm_prompt_contains_only_command_and_participants(llm_assistant):
    llm_assistant.handle_text("Mehmet raporu bitirdi mi bilmiyorum.")  # transkript; LLM'ye gönderilmez
    llm_assistant.handle_text("Jarvis, gündemde neler var acaba")
    payload = llm_assistant.fake.calls[-1]["user"]
    assert payload["komut"] == "gündemde neler var acaba"
    assert "Mehmet raporu" not in str(payload)
