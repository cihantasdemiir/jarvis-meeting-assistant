"""Faz 5c.1 düzeltmesi: gerçek qwen3.5:4b doğal set ölçümünde (32/36) görülen hata SINIFLARI.

D09/D10 (model kararsız: NONE), D14 (okuma niyeti + kayıt isteği), D35 (bağlam + istek), D17 (kategori),
D13 (görev metni).
Doğal setteki cümleler burada KULLANILMADI (yalnızca D-numaralı gerçek model çıktılarının yeniden oynatıldığı tek test
hariç); farklı adlar, fiiller ve cümle yapıları kullanılır. Sahte model gerçek Qwen doğruluğunu ölçmez.
"""
from __future__ import annotations

from datetime import date

import pytest

from jarvis.llm import benchmark as bench
from jarvis.llm.intent_cues import agenda_title, lexical_intent, request_clause
from jarvis.llm.router import READ_WRITE_MESSAGE, LlmCommandRouter, strip_command_words
from jarvis.voice.command_parser import CommandParser, IntentType
from tests.llm_fakes import FakeLlm

PARSER = CommandParser("jarvis", ["carvis"])
TEAM = ["Gizem Ateş", "Onur Kaya", "Sena Polat", "Barış Uz", "Sena Demir"]
DAY = date(2026, 10, 21)


def model(intent, category=None, confidence=0.9, **arguments):
    data = {"intent": intent, "arguments": arguments, "confidence": confidence}
    if category:
        data["category"] = category
    return data


def route(answer, command, people=TEAM):
    fake = FakeLlm(answer)
    return LlmCommandRouter(fake, "m", PARSER).route(command, people, DAY), fake


# ====================================================================== gündeme ekleme (D09 sınıfı)
@pytest.mark.parametrize("command,title", [
    ("Gündeme kalite kontrolü ekleyelim", "kalite kontrolü"),
    ("Bir sonraki başlığımız tedarik planlaması olsun", "tedarik planlaması"),
    ("Gündemimize sevkiyat gecikmelerini de dahil eder misin?", "sevkiyat gecikmeleri"),
    ("Son madde olarak müşteri şikayetlerini ekleyebilir miyiz?", "müşteri şikayetleri"),
    ("gündeme stok sayımını da koyalım", "stok sayımı"),
    ("yeni başlık olarak bakım planını alalım", "bakım planı"),
    ("depo düzeni maddesini gündemimize ekler misin", "depo düzeni"),
])
@pytest.mark.parametrize("answer", [
    model("NONE", "chat", 0.1), model("NONE", "unclear", 0.2), model("NONE"),
    model("READ_AGENDA", "question", 0.3),
])
def test_agenda_add_request_is_proposed_even_if_model_abstains(command, title, answer):
    decision, _ = route(answer, command)
    assert decision.kind == "confirm" and decision.intent.type == IntentType.ADD_AGENDA_ITEM
    assert decision.intent.get("title") == title and decision.canonical == f"gündeme ekle: {title}"
    assert "Jarvis, evet" in decision.message  # yeni madde yalnızca onayla eklenir


def test_model_chosen_agenda_add_with_missing_title_uses_users_words():
    decision, _ = route(model("ADD_AGENDA_ITEM", title=None), "gündemimize iş güvenliği eğitimini ekleyelim")
    assert decision.kind == "confirm" and decision.intent.get("title") == "iş güvenliği eğitimi"


@pytest.mark.parametrize("command,category", [
    ("gündeme stok sayımını eklemeyelim", "negated"),
    ("eğer vakit kalırsa gündeme stok sayımını ekleriz", "hypothetical"),
    ("gündeme stok sayımını eklemeli miyiz?", "question"),
    ("sence bakım planı gündemde olmalı mı", "question"),
])
@pytest.mark.parametrize("answer", [model("NONE", "chat", 0.1), model("ADD_AGENDA_ITEM", title="stok sayımı")])
def test_agenda_add_never_from_negated_hypothetical_or_deliberative(command, category, answer):
    decision, _ = route(answer, command)
    assert decision.kind == "no_action" and decision.intent is None and decision.category == category


# ====================================================================== gündemi okuma (D10 sınıfı)
@pytest.mark.parametrize("command", [
    "Bugün neleri görüşeceğiz?", "Toplantının konularını hatırlatır mısın?", "Sırada hangi gündem maddeleri var?",
    "Gündemimizde neler bulunuyor?", "masadaki konuları bir sıralar mısın", "bugün ele alacağımız maddeler neler",
])
@pytest.mark.parametrize("answer", [model("NONE", "unclear", 0.1), model("NONE", "question"), model("READ_AGENDA")])
def test_agenda_read_requests_execute_read_only(command, answer):
    decision, _ = route(answer, command)
    assert decision.kind == "execute" and decision.intent.type == IntentType.READ_AGENDA


def test_reading_never_creates_an_agenda_item():
    for command in ("Bugün neleri görüşeceğiz?", "Sırada hangi gündem maddeleri var?"):
        assert lexical_intent(command) == IntentType.READ_AGENDA and agenda_title(command) is None


def test_true_chat_and_unrelated_text_stay_non_actions():
    for command, category in (("bugün hava çok güzel değil mi", "chat"), ("dün maç çok iyiydi", "chat")):
        decision, _ = route(model("NONE", category, 0.4), command)
        assert decision.kind == "no_action" and decision.intent is None


# ====================================================================== okuma niyeti + kayıt isteği (D14 sınıfı)
@pytest.mark.parametrize("command,names,expected", [
    ("Barış'ı da yoklamaya ekle", ["Barış"], ["Barış Uz"]),
    ("Gizem ve Onur de geldi, kaydeder misin?", ["Gizem", "Onur"], ["Gizem Ateş", "Onur Kaya"]),
    ("misafirimiz Kaan'ı katılımcılara işaretler misin", ["Kaan"], ["Kaan"]),
])
def test_write_request_is_not_silently_turned_into_read(command, names, expected):
    decision, _ = route(model("ATTENDANCE", "command", 0.95, names=names), command)
    assert decision.kind == "confirm"
    assert decision.intent.type in (IntentType.MARK_PRESENT_MANY, IntentType.MARK_PRESENT)
    assert decision.intent.get("names") == expected and "Jarvis, evet" in decision.message
    assert decision.llm_intent == "ATTENDANCE"  # tanılamada modelin gerçek seçimi görünür


def test_write_request_without_names_asks_instead_of_reading():
    decision, _ = route(model("ATTENDANCE", "command", 0.95), "yoklamaya ekler misin")
    assert decision.kind == "clarify" and decision.intent is None


def test_other_read_intent_with_write_request_asks():
    decision, _ = route(model("OPEN_ACTIONS", "command", 0.9), "açık görevlere bir tane daha ekle")
    assert decision.kind == "clarify" and decision.message == READ_WRITE_MESSAGE and decision.intent is None


@pytest.mark.parametrize("command", ["Kimler katıldı?", "Barış toplantıda mı?", "eksik kimse var mı"])
def test_attendance_questions_stay_read_only(command):
    decision, _ = route(model("ATTENDANCE", "question"), command)
    assert decision.kind == "execute" and decision.intent.type == IntentType.ATTENDANCE


# ====================================================================== bağlam + istek (D35 sınıfı)
@pytest.mark.parametrize("command,answer,expected", [
    ("Onur sunumunu tamamladı, bir sonraki başlığa geçebiliriz", model("RELEASE_SPEAKER", speaker="Onur"),
     IntentType.NEXT_ITEM),
    ("Gizem bitirdi, sıradaki konuya geçelim", model("RELEASE_SPEAKER"), IntentType.NEXT_ITEM),
    ("Gizem bitirdi, sözü kapat", model("RELEASE_SPEAKER"), IntentType.RELEASE_SPEAKER),
    ("Gizem bitirdi, sözü kapat", model("NEXT_ITEM"), IntentType.RELEASE_SPEAKER),
    ("Barış'ın sunumu uzadı, şimdilik diğer maddeye geçelim", model("SKIP_ITEM"), IntentType.NEXT_ITEM),
])
def test_intent_comes_from_request_part(command, answer, expected):
    decision, _ = route(answer, command)
    assert decision.kind == "confirm" and decision.intent.type == expected


@pytest.mark.parametrize("command,answer,category", [
    ("Onur'a söz ver, sonra gündemi oku", model("SET_SPEAKER", speaker="Onur"), "multi"),
    ("Onur bitirdi ama henüz sonraki maddeye geçmeyelim", model("NEXT_ITEM"), "negated"),
    ("Sonraki konuya geçsek mi, yoksa burada biraz daha durmalı mıyız?", model("NEXT_ITEM"), "question"),
])
def test_compound_sentences_keep_safe_behaviour(command, answer, category):
    decision, _ = route(answer, command)
    assert decision.kind == "no_action" and decision.category == category and decision.intent is None


def test_request_part_hint_is_sent_to_model_only_for_compound_sentences():
    _, fake = route(model("NEXT_ITEM"), "Gizem bitirdi, sıradaki konuya geçelim")
    assert fake.calls[0]["user"]["istek_bolumu"] == "sıradaki konuya geçelim"
    _, fake = route(model("NEXT_ITEM"), "sıradaki konuya geçelim mi")
    assert "istek_bolumu" not in fake.calls[0]["user"]
    assert request_clause("Gizem bitirdi, Onur başladı") is None  # istek yok


# ====================================================================== kategori ve görev metni
@pytest.mark.parametrize("command", ["bu işi Onur'a vermeli miyiz", "sence Sena bu raporu yetiştirir mi",
                                     "acaba bütçeyi kısmalı mıyız"])
def test_deliberative_question_is_question_not_chat(command):
    decision, _ = route(model("NONE", "chat", 0.6), command)
    assert decision.kind == "no_action" and decision.category == "question" and decision.intent is None


@pytest.mark.parametrize("text,expected", [
    ("bütçe tablosunu güncelleme görevi ver", "bütçe tablosunu güncelleme"),
    ("tedarikçi listesini çıkarma işini ata", "tedarikçi listesini çıkarma"),
    ("teklifi hazırlasın diye görev aç", "teklifi hazırlasın"),
    ("raporu hazırlasın", "raporu hazırlasın"),
    ("görevi ver", "görevi ver"),  # tamamen silinmez
])
def test_command_words_removed_from_task_text(text, expected):
    assert strip_command_words(text) == expected


def test_task_keeps_owner_and_due_after_cleanup():
    decision, _ = route(model("ADD_ACTION", text="müşteri listesini perşembeye kadar düzeltme görevi ver",
                              owner="Barış", date_text="perşembeye kadar"),
                        "Barış'a müşteri listesini perşembeye kadar düzeltme görevi ver")
    assert decision.kind == "confirm" and "görevi ver" not in decision.canonical
    assert "Sorumlu: Barış Uz" in decision.message and "22 Ekim 2026" in decision.message


# ====================================================================== güvenlik değişmedi
def test_cues_never_unlock_explicit_only_operations():
    decision, _ = route(model("END_MEETING", confidence=0.2), "gündemi bitirelim artık, sıradaki maddeye geçelim")
    assert decision.kind == "blocked" and decision.intent is None


def test_low_confidence_without_clear_cue_still_makes_no_proposal():
    decision, _ = route(model("ADD_DECISION", confidence=0.2, text="bütçe"), "bütçe meselesi hallolmuş gibi")
    assert decision.kind == "clarify" and decision.intent is None


def test_ambiguous_person_still_asked_after_switch():
    decision, _ = route(model("ATTENDANCE", names=["Sena"]), "Sena'yı da yoklamaya ekle")
    assert decision.kind == "clarify" and "Sena Polat" in decision.message and "Sena Demir" in decision.message


def test_replay_of_real_qwen_outputs_on_natural_set():
    """Kullanıcının bildirdiği gerçek qwen3.5:4b çıktılarının yeniden oynatılması (yeni model ölçümü değildir)."""
    real = {
        "D09": model("NONE", "chat", 0.1, text=None, title=None, minutes=None),
        "D10": model("NONE", "unclear", 0.1, text=None, title=None, minutes=None),
        "D14": model("ATTENDANCE", "command", 0.95, names=["Burcu Aksoy", "Kerem Tan", "Emre"]),
        "D35": model("RELEASE_SPEAKER", "command", 0.95, speaker="Tolga Yıldız"),
    }
    data = bench.load_benchmark(bench.NATURAL_PATH)
    cases = [c for c in data["router"] if c["id"] in real]

    def responder(_system, user, _schema):
        return real[next(c["id"] for c in cases if user["komut"] in c["text"])]
    results = bench.run_router_cases(cases, PARSER, LlmCommandRouter(FakeLlm(responder), "q", PARSER), "q",
                                     data["participants"], date.fromisoformat(data["meeting_date"]),
                                     data["agenda_item"])
    assert all(r.passed for r in results), [(r.case_id, r.detail) for r in results]
    assert not any(r.safety_violation or r.unsafe_proposal for r in results)


def test_benchmark_expectations_unchanged():
    sizes = [sum(len(bench.load_benchmark(p)[k]) for k in ("router", "extract", "summary"))
             for p in (bench.BENCHMARK_PATH, bench.HOLDOUT_PATH, bench.EXTRA_PATH, bench.NATURAL_PATH)]
    assert sizes == [106, 46, 20, 36]
