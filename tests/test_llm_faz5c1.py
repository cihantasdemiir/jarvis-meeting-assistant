"""Faz 5c.1: doğal Türkçe ifadeleri anlama ve güvenli yönlendirme.

Gerçek hata: "Jarvis, Ayşe Mehmet ve Furkan katıldı" → "Ayşe Mehmet V Furkan" adlı TEK kişi ekleniyordu.
Sahte model (FakeLlm) kullanılır; bu testler gerçek Qwen doğruluğunu ÖLÇMEZ, doğrulama ve güvenlik katmanını ölçer.
Katılımcılar (conftest): Kenan Çetin, Ayşe Yılmaz, Mehmet Öztürk, Ahmet Yılmaz.
"""
from __future__ import annotations

from datetime import date

import pytest

from jarvis.llm import benchmark as bench
from jarvis.llm.analyzer import MeetingAnalyzer
from jarvis.llm.router import (
    CHAT_MESSAGE,
    QUESTION_INFO_MESSAGE,
    ROUTER_SCHEMA,
    UNCLEAR_MESSAGE,
    LlmCommandRouter,
)
from jarvis.llm.schema import SchemaError, validate
from jarvis.llm.services import LlmServices
from jarvis.llm.utterance_guard import (
    count_requests,
    is_negated,
    post_llm_guard,
    pre_llm_guard,
)
from jarvis.voice.command_parser import MULTI_COMMAND_MESSAGE, NEGATED_MESSAGE, CommandParser, IntentType
from jarvis.utils.turkish_text import expand_name_chunks, split_names
from tests.llm_fakes import FakeLlm, router_answer

PARSER = CommandParser("jarvis", ["carvis"])
PEOPLE = ["Kenan Çetin", "Ayşe Yılmaz", "Mehmet Öztürk", "Ahmet Yılmaz"]
DAY = date(2026, 10, 14)


def route(answer, command, people=PEOPLE, **kwargs):
    fake = FakeLlm(answer)
    return LlmCommandRouter(fake, "m", PARSER, **kwargs).route(command, people, DAY), fake


def answer(intent, category=None, confidence=0.9, **arguments):
    data = {"intent": intent, "arguments": arguments, "confidence": confidence}
    if category:
        data["category"] = category
    return data


# ====================================================================== isim listeleri
@pytest.mark.parametrize("text,expected", [
    ("Ayşe", ["Ayşe"]),
    ("Ayşe, Mehmet ve Furkan", ["Ayşe", "Mehmet", "Furkan"]),
    ("Ayşe Mehmet ve Furkan", ["Ayşe Mehmet", "Furkan"]),          # bitişik kısım listeyle çözülür
    ("Ayşe Mehmet V Furkan", ["Ayşe Mehmet", "Furkan"]),           # gerçek STT çıktısı
    ("Ayşe v Mehmet v Furkan", ["Ayşe", "Mehmet", "Furkan"]),
    ("Ayşe ile Mehmet", ["Ayşe", "Mehmet"]),
    ("Ayşe; Mehmet & Furkan", ["Ayşe", "Mehmet", "Furkan"]),
    ("Ayşe de", ["Ayşe"]),
    ("Ayşe ve Mehmet de", ["Ayşe", "Mehmet"]),
])
def test_split_names_conjunctions(text, expected):
    assert split_names(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Veli ve Vildan", ["Veli", "Vildan"]),           # "v" ile başlayan isimler bozulmaz
    ("Ali V. Demir ve Can", ["Ali V. Demir", "Can"]),  # noktalı baş harf ayraç değildir
    ("İlker, Ilgın ve Deniz", ["İlker", "Ilgın", "Deniz"]),
    ("Vedat İleri", ["Vedat İleri"]),                  # "ile" kelime içinde ayraç değildir
    ("Dede Korkut", ["Dede Korkut"]),
])
def test_split_names_never_corrupts_real_names(text, expected):
    assert split_names(text) == expected


def test_adjacent_names_split_only_when_every_word_is_a_known_person():
    assert expand_name_chunks(["Ayşe Mehmet", "Furkan"], PEOPLE) == ["Ayşe", "Mehmet", "Furkan"]
    assert expand_name_chunks(["Ayşe Furkan"], PEOPLE) == ["Ayşe Furkan"]  # tahmin yok
    assert expand_name_chunks(["Ayşe Yılmaz"], PEOPLE) == ["Ayşe Yılmaz"]  # ad-soyad bölünmez


# ====================================================================== hızlı ayrıştırıcı
@pytest.mark.parametrize("text,intent,names", [
    ("Jarvis, Ayşe Mehmet ve Furkan katıldı.", IntentType.MARK_PRESENT_MANY, ["Ayşe Mehmet", "Furkan"]),
    ("Jarvis Ayşe Mehmet V Furkan katıldı", IntentType.MARK_PRESENT_MANY, ["Ayşe Mehmet", "Furkan"]),
    ("Jarvis, Ayşe de geldi.", IntentType.MARK_PRESENT, ["Ayşe"]),
    ("Jarvis, Ayşe'nin yoklamasını al.", IntentType.MARK_PRESENT, ["Ayşe"]),
    ("Jarvis, Mehmet toplantıya katıldı", IntentType.MARK_PRESENT, ["Mehmet"]),
    ("Jarvis, katılımcılar: Ayşe, Mehmet ve Furkan'ı", IntentType.MARK_PRESENT_MANY, ["Ayşe", "Mehmet", "Furkan"]),
])
def test_clear_attendance_commands_are_fast_rules(text, intent, names):
    parsed = PARSER.parse(text)
    assert parsed.type == intent and parsed.get("names") == names


@pytest.mark.parametrize("text", [
    "Jarvis, Ayşe geldi mi?",            # soru: yoklama yapılmaz
    "Jarvis, Ayşe gelmedi",              # olumsuz
    "Jarvis, Ayşenin yoklamasını al",    # kesme işareti yok: Ayşe mi Ayşen mi belli değil → LLM + onay
])
def test_attendance_rule_does_not_guess(text):
    assert PARSER.parse(text).type == IntentType.UNKNOWN


def test_two_record_commands_in_one_sentence_are_not_recorded():
    parsed = PARSER.parse("Jarvis, karar: bütçe onaylandı ve görev: Mehmet raporu hazırlasın")
    assert parsed.type == IntentType.UNKNOWN and parsed.get("clarify") == MULTI_COMMAND_MESSAGE


@pytest.mark.parametrize("text", ["Jarvis, karar: bunu henüz kaydetme", "Jarvis görev: şimdilik yazma"])
def test_negated_record_text_is_not_recorded(text):
    parsed = PARSER.parse(text)
    assert parsed.type == IntentType.UNKNOWN and parsed.get("clarify") == NEGATED_MESSAGE


def test_existing_fast_commands_unchanged():
    assert PARSER.parse("Jarvis, karar: yeni üretim planı pazartesi uygulanacak").type == IntentType.ADD_DECISION
    assert PARSER.parse("Jarvis, karar: taşeronla çalışma kararı alındı").get("text") == \
        "taşeronla çalışma kararı alındı"
    assert PARSER.parse("Jarvis söz Ayşe'de").type == IntentType.SET_SPEAKER
    assert PARSER.parse("Jarvis, gündemi oku").type == IntentType.READ_AGENDA


# ====================================================================== asistan: yoklama akışı
@pytest.fixture
def started(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


def present(assistant):
    return sorted(p.name for p in assistant.manager.meeting.participants if p.present)


def test_real_bug_three_people_detected_separately(started):
    response = started.handle_text("Jarvis, Ayşe Mehmet V Furkan katıldı.")
    assert present(started) == ["Ayşe Yılmaz", "Furkan", "Mehmet Öztürk"]
    assert "Ayşe Mehmet V Furkan" not in [p.name for p in started.manager.meeting.participants]
    assert "3 katılımcının" in response.message


def test_single_name(started):
    started.handle_text("Jarvis, Ayşe de geldi.")
    assert present(started) == ["Ayşe Yılmaz"]


@pytest.mark.parametrize("text,uncertain", [
    ("Jarvis, Selin Kaya katıldı", "Selin Kaya"),          # ad-soyad mı, iki kişi mi?
    ("Jarvis, Ayşe Furkan katıldı", "Ayşe Furkan"),        # Ayşe bilinen, Furkan değil: varsayım yok
])
def test_name_surname_ambiguity_asks_before_adding(started, text, uncertain):
    before = len(started.manager.meeting.participants)
    response = started.handle_text(text)
    assert len(started.manager.meeting.participants) == before and started.pending is not None
    assert uncertain in response.message and "Jarvis, evet" in response.message
    started.handle_text("Jarvis evet")
    assert uncertain in [p.name for p in started.manager.meeting.participants]


def test_ambiguity_rejected_adds_nobody(started):
    before = len(started.manager.meeting.participants)
    started.handle_text("Jarvis, Selin Kaya katıldı")
    started.handle_text("Jarvis hayır")
    assert len(started.manager.meeting.participants) == before


def test_non_person_in_implicit_form_is_asked(started):
    before = len(started.manager.meeting.participants)
    response = started.handle_text("Jarvis, rapor geldi")
    assert len(started.manager.meeting.participants) == before and "emin değilim" in response.message


def test_direct_api_behaviour_unchanged(manager):
    manager.start_meeting()
    manager.mark_present(["zeynep kaya"])
    assert "Zeynep Kaya" in [p.name for p in manager.meeting.participants]


# ====================================================================== LLM yönlendirme: doğal komutlar
@pytest.mark.parametrize("command,model,kind,intent", [
    ("Mehmet'e söz verelim", answer("SET_SPEAKER", speaker="Mehmet"), "confirm", IntentType.SET_SPEAKER),
    ("Mehmet konuşmasını bitirdi", answer("RELEASE_SPEAKER"), "confirm", IntentType.RELEASE_SPEAKER),
    ("gündemimize kalite kontrolü ekleyelim", answer("ADD_AGENDA_ITEM", title="kalite kontrolü"), "confirm",
     IntentType.ADD_AGENDA_ITEM),
    ("bugün hangi konuları görüşeceğiz?", answer("READ_AGENDA", "question"), "execute", IntentType.READ_AGENDA),
    ("son görevlerin durumunu söyle", answer("OPEN_ACTIONS"), "execute", IntentType.OPEN_ACTIONS),
    ("Mehmet'e rapor hazırlama görevi ver", answer("ADD_ACTION", text="rapor hazırlama", owner="Mehmet"),
     "confirm", IntentType.ADD_ACTION),
    ("Mehmet'e söz verebilir misin", answer("SET_SPEAKER", speaker="Mehmet"), "confirm", IntentType.SET_SPEAKER),
])
def test_natural_commands_route_through_existing_safety_chain(command, model, kind, intent):
    decision, fake = route(model, command)
    assert len(fake.calls) == 1 and decision.kind == kind and decision.intent.type == intent
    assert decision.intent.get("via_llm") is True


def test_three_people_via_llm_includes_new_person_after_confirmation(started):
    fake = FakeLlm(answer("MARK_PRESENT_MANY", names=["Ayşe", "Mehmet", "Furkan"]))
    started.llm = LlmServices(client=fake, model="m", router=LlmCommandRouter(fake, "m", started.parser),
                              analyzer=MeetingAnalyzer(fake, "m"))
    response = started.handle_text("Jarvis, Ayşe, Mehmet ve Furkan'ı ekle.")
    assert "yeni eklenecek: Furkan" in response.message and present(started) == []
    started.handle_text("Jarvis evet")
    assert present(started) == ["Ayşe Yılmaz", "Furkan", "Mehmet Öztürk"]


@pytest.mark.parametrize("names,reason", [
    (["Ayşe", "Mehmet Furkan"], "listede yok"),     # model iki kişiyi birleştirdi
    (["Ayşe", "Zeynep"], "uydurma"),                # cümlede geçmeyen kişi
])
def test_llm_cannot_merge_or_invent_people(names, reason):
    decision, _ = route(answer("MARK_PRESENT_MANY", names=names), "Ayşe, Mehmet ve Furkan'ı ekle")
    assert decision.kind in ("clarify", "unknown") and decision.intent is None


# ====================================================================== olumsuz, soru, varsayım, sohbet
@pytest.mark.parametrize("command", [
    "bu kararı henüz kaydetme", "Ayşe'yi yoklamaya ekleme", "şimdilik yazma", "toplantıyı başlatma",
    "bunu not almaya gerek yok", "görev oluşturmayalım",
])
def test_negated_requests_never_reach_model(command):
    decision, fake = route(answer("ADD_DECISION", text=command), command)
    assert decision.kind == "no_action" and decision.category == "negated" and fake.calls == []


@pytest.mark.parametrize("command,intent,args", [
    ("Mehmet'e bu görevi vermeli miyiz?", "ADD_ACTION", {"text": "bu görevi", "owner": "Mehmet"}),
    ("Ayşe geldi mi?", "MARK_PRESENT_MANY", {"names": ["Ayşe"]}),
    ("sence bu kararı kaydedelim mi", "ADD_DECISION", {"text": "bu kararı"}),
    ("Mehmet raporu hazırlarsa haftaya bakarız", "ADD_ACTION", {"text": "raporu hazırlarsa", "owner": "Mehmet"}),
    ("eğer bütçe onaylanırsa karar olarak yaz", "ADD_DECISION", {"text": "bütçe onaylanırsa"}),
])
def test_question_or_hypothetical_never_becomes_a_record(command, intent, args):
    decision, _ = route(answer(intent, **args), command)
    assert decision.kind == "no_action" and decision.intent is None
    assert decision.category in ("question", "hypothetical")


def test_questions_can_still_read_information():
    decision, _ = route(answer("ATTENDANCE", "question"), "toplantıda kimler var?")
    assert decision.kind == "execute" and decision.intent.type == IntentType.ATTENDANCE


@pytest.mark.parametrize("command", ["teşekkürler", "teşekkür ederim Jarvis", "günaydın", "nasılsın"])
def test_chat_is_never_an_action_and_needs_no_model(command):
    decision, fake = route(answer("ADD_NOTE", text=command), command)
    assert decision.kind == "no_action" and decision.category == "chat" and fake.calls == []


@pytest.mark.parametrize("category,message,kind", [
    ("chat", CHAT_MESSAGE, "no_action"),
    ("question", QUESTION_INFO_MESSAGE, "no_action"),
    ("unclear", UNCLEAR_MESSAGE, "clarify"),
])
def test_model_categories_for_non_commands(category, message, kind):
    decision, _ = route(answer("NONE", category), "bugün hava çok güzel değil mi")
    assert decision.kind == kind and decision.message == message and decision.intent is None


def test_multiple_actions_in_one_sentence_need_separate_commands():
    decision, fake = route(answer("SET_SPEAKER", speaker="Ayşe"), "Ayşe'ye söz ver ve Mehmet'i yoklamaya ekle")
    assert decision.kind == "no_action" and decision.category == "multi" and fake.calls == []
    assert count_requests("Selim raporu bitirsin, görev olarak ekle") == 1  # kayıt talimatı ayrı işlem değildir


# ====================================================================== model hataları ve yetki
def test_low_confidence_makes_no_proposal():
    decision, _ = route(answer("ADD_DECISION", confidence=0.3, text="bütçe onaylandı"), "bütçe onaylandı galiba")
    assert decision.kind == "clarify" and decision.intent is None and "Emin olamadım" in decision.message


def test_low_confidence_threshold_is_configurable():
    decision, _ = route(answer("READ_AGENDA", confidence=0.3), "konular neydi", min_confidence=0.2)
    assert decision.kind == "execute"


@pytest.mark.parametrize("bad", [
    {"intent": "READ_AGENDA", "arguments": {}, "confidence": 0.9, "category": "eğlence"},  # şema dışı kategori
    {"intent": "DELETE_DATABASE", "arguments": {}, "confidence": 0.9},                    # izinsiz niyet
    {"intent": "ADD_NOTE", "arguments": {"sql": "DROP TABLE"}, "confidence": 0.9},        # şema dışı alan
    {"arguments": {}, "confidence": 0.9},                                                 # eksik alan
])
def test_invalid_or_out_of_schema_output_is_rejected(bad):
    with pytest.raises(SchemaError):
        validate(bad, ROUTER_SCHEMA)
    decision, _ = route(bad, "gündemi bir okur musun")
    assert decision.kind == "error" and decision.intent is None and not decision.json_valid


def test_broken_json_from_model_falls_back_safely():
    from jarvis.llm.client import LlmError

    decision, _ = route(LlmError("model geçerli JSON döndürmedi"), "gündemi bir okur musun")
    assert decision.kind == "error" and decision.intent is None and "anlayamadım" in decision.message


def test_unauthorized_operation_is_blocked():
    decision, _ = route(answer("SEND_MINUTES"), "herkese bir kopya yollasak iyi olur")
    assert decision.kind == "blocked" and decision.intent is None


def test_old_answers_without_category_still_valid():
    decision, _ = route(router_answer("READ_AGENDA"), "konular neler")
    assert decision.kind == "execute"


def test_latency_is_measured_for_llm_calls():
    decision, _ = route(answer("READ_AGENDA"), "konular neler")
    assert decision.latency_s >= 0.0
    decision, _ = route(answer("READ_AGENDA"), "teşekkürler")
    assert decision.latency_s == 0.0  # modele sorulmadı


# ====================================================================== LLM kapalı / hitapsız konuşma
def test_llm_off_explicit_commands_and_guards_still_work(started):
    assert started.llm.router is None
    assert started.handle_text("Jarvis, karar: bütçe onaylandı").intent == IntentType.ADD_DECISION
    response = started.handle_text("Jarvis, bu kararı henüz kaydetme")
    assert "hiçbir işlem" in response.message and len(started.manager.meeting.decisions) == 1
    assert "Rica ederim" in started.handle_text("Jarvis, teşekkürler").message


def test_unaddressed_meeting_talk_stays_in_transcript_and_never_reaches_model(started):
    fake = FakeLlm(answer("ADD_ACTION", text="raporu hazırlasın", owner="Mehmet"))
    started.llm = LlmServices(client=fake, model="m", router=LlmCommandRouter(fake, "m", started.parser),
                              analyzer=MeetingAnalyzer(fake, "m"))
    response = started.handle_text("Mehmet raporu hazırlasın.")
    assert response.is_command is False and fake.calls == []
    assert started.manager.meeting.transcript[-1].text == "Mehmet raporu hazırlasın."
    assert started.manager.meeting.action_items == []


def test_pre_and_post_guard_units():
    assert is_negated("bu kararı henüz kaydetme") and not is_negated("gündeme ekleme yapalım")
    assert pre_llm_guard("Mehmet'e söz verelim") is None
    assert post_llm_guard("sıradaki maddeye geçelim mi") is None  # "-elim mi" bir öneridir


# ====================================================================== değerlendirme kümesi
def test_natural_set_is_separate_and_marked_developer_made():
    data = bench.load_benchmark(bench.NATURAL_PATH)
    assert len(data["router"]) == 36 and data["extract"] == [] and "GELİŞTİRİCİ" in data["description"]
    ids = {c["id"] for c in data["router"]}
    for other in (bench.BENCHMARK_PATH, bench.HOLDOUT_PATH, bench.EXTRA_PATH):
        assert not ids & {c["id"] for c in bench.load_benchmark(other)["router"]}


def test_natural_set_rule_baseline_has_no_safety_violation():
    data = bench.load_benchmark(bench.NATURAL_PATH)
    results = bench.run_router_cases(data["router"], PARSER, None, "kural", data["participants"],
                                     date.fromisoformat(data["meeting_date"]), data["agenda_item"])
    assert not any(r.safety_violation or r.unsafe_proposal for r in results)
    by_id = {r.case_id: r for r in results}
    for case_id in ("D01", "D02", "D06", "D19", "D22", "D25", "D30", "D31"):  # LLM gerektirmeyenler
        assert by_id[case_id].passed, (case_id, by_id[case_id].detail)


def test_existing_benchmark_sizes_unchanged():
    sizes = [sum(len(bench.load_benchmark(p)[k]) for k in ("router", "extract", "summary"))
             for p in (bench.BENCHMARK_PATH, bench.HOLDOUT_PATH, bench.EXTRA_PATH)]
    assert sizes == [106, 46, 20]
