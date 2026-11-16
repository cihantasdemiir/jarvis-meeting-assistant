"""Faz 5c.2 canlı kullanım bulguları için regresyon testleri.

Kullanıcının `python main.py --text` / `python main.py` denemelerinde görülen hatalar:
  1. "sorumlu yok / tarih yok" yanıtı sonradan kişi adı ("Yok Son Tarih Yok") olarak görev sorumlusuna yazıldı.
  2. "Jarvis" duyulmadan söylenen komut kalıpları transkripte sıradan konuşma olarak girdi.
  3. "söz Mehmet'te" sonrası süre, gerçek konuşma ölçümü gibi raporlandı; program kapalıyken geçen süre de sayıldı.
  4. Çok sayıda "anlayamadım": tanılama yoktu, canlı oturumda LLM'nin kapalı olduğu görünmüyordu.
Gerçek mikrofon, STT veya Qwen kullanılmaz.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config.settings import Settings, load_settings
from jarvis.diagnostics import PipelineTrace, trace_report
from jarvis.llm.analyzer import build_lines
from jarvis.llm.services import config_report
from jarvis.meeting.manager import MeetingManager
from jarvis.meeting.models import Meeting, SpeakerTurn, TranscriptEntry, TranscriptKind
from jarvis.meeting.speaker_tracker import SpeakerTracker
from jarvis.utils.turkish_text import is_none_value, plausible_person_name
from jarvis.voice.command_parser import CommandParser, IntentType
from jarvis.voice.unaddressed import classify_unaddressed
from jarvis.voice.voice_listener import Utterance

PARSER = CommandParser("jarvis", ["carvis"])


@pytest.fixture
def started(assistant):
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


def stored(assistant) -> Meeting:
    return assistant.manager.repo.get(assistant.manager.meeting.id)


def actions(meeting: Meeting) -> list[tuple[str, str, object]]:
    return [(a.text, a.owner, a.due_date) for a in meeting.action_items]


def pdf_text(path: str) -> str:
    pypdf = pytest.importorskip("pypdf")
    return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(path).pages)


# ====================================================================== 1. sorumlu yok / tarih yok
def test_live_scenario_owner_and_due_none(started, minutes):
    a = started
    a.handle_text("Jarvis, görev ekle.")
    a.handle_text("Boruların bakımı yapılacak.")
    a.handle_text("Sorumlu yok.")
    r = a.handle_text("Tarih yok.")
    assert "Sorumlu: belirtilmedi. Son tarih: belirtilmedi" in r.message
    r = a.handle_text("Evet.")
    assert "Sorumlu: belirtilmedi." in r.message and "anlaşılamadı" not in r.message
    expected = [("Boruların bakımı yapılacak", "", None)]
    assert actions(a.manager.meeting) == expected and actions(stored(a)) == expected
    pdf = minutes.create_pdf(a.manager.meeting)
    text = pdf_text(pdf.data["path"])
    assert "Boruların bakımı yapılacak" in text and "Belirlenmedi" in text
    assert "Yok Son Tarih" not in text and "yok son tarih" not in text.lower()


@pytest.mark.parametrize("answer", ["sorumlu yok son tarih yok", "Sorumlu yok, son tarih yok.",
                                    "Jarvis, sorumlu yok, son tarih yok", "Jarvis sorumlu yok son tarih yok"])
def test_combined_none_answer_is_two_fields(started, answer):
    a = started
    a.handle_text("Jarvis, görev ekle")
    a.handle_text("Boruların bakımı yapılacak")
    r = a.handle_text(answer)
    assert "Sorumlu: belirtilmedi. Son tarih: belirtilmedi" in r.message
    a.handle_text("evet")
    assert actions(stored(a)) == [("Boruların bakımı yapılacak", "", None)]


def test_combined_answer_never_touches_previous_task(started):
    """Kök neden: anlaşılamayan "Jarvis, sorumlu yok, son tarih yok" soru düşürülüp SET_ACTION_OWNER olarak
    ÖNCEKİ göreve uygulanıyordu."""
    a = started
    a.handle_text("Jarvis, görev: Ayşe sunumu cumaya kadar hazırlasın")
    a.handle_text("Jarvis, görev ekle")
    a.handle_text("Boruların bakımı yapılacak")
    a.handle_text("Jarvis, sorumlu yok, son tarih belli değil sanırım")  # anlaşılmadı → yeniden sorulur
    assert a.manager.meeting.action_items[0].owner == "Ayşe Yılmaz"
    assert a.dialog.question is not None and a.dialog.question.kind == "task_owner"
    assert len(a.manager.meeting.action_items) == 1


@pytest.mark.parametrize("answer,owner,due", [
    ("Jarvis, sorumlu Mehmet, son tarih cuma", "Mehmet Öztürk", "16 Ekim 2026"),
    ("Ayşe tarih yarın", "Ayşe Yılmaz", "15 Ekim 2026"),
    ("sorumlu yok, son tarih haftaya", "belirtilmedi", "21 Ekim 2026"),
])
def test_combined_owner_and_due_answers(started, answer, owner, due):
    started.handle_text("Jarvis, görev ekle")
    started.handle_text("vana değişimi yapılacak")
    r = started.handle_text(answer)
    assert f"Sorumlu: {owner}. Son tarih: {due}" in r.message


@pytest.mark.parametrize("command", ["Jarvis, sorumlu yok son tarih yok", "Jarvis, sorumlu yok",
                                     "Jarvis, son tarih yok", "Jarvis, sorumlu: Mehmet son tarih cuma"])
def test_late_field_answer_after_confirmation_changes_nothing(started, command):
    a = started
    a.handle_text("Jarvis, görev ekle")
    a.handle_text("Boruların bakımı yapılacak")
    a.handle_text("sorumlu yok")
    a.handle_text("tarih yok")
    a.handle_text("evet")
    before = actions(stored(a))
    r = a.handle_text(command)
    assert r.ok is False
    assert actions(a.manager.meeting) == before and actions(stored(a)) == before


def test_repeated_answers_after_dialog_do_not_start_new_operation(started):
    a = started
    a.handle_text("Jarvis, karar ekle")
    a.handle_text("bütçe onaylandı")
    a.handle_text("evet")
    snapshot = json.dumps(stored(a).to_dict(), sort_keys=True, default=str)
    for late in ("evet", "Evet.", "tarih yok", "sorumlu yok"):
        r = a.handle_text(late)
        assert r.is_command is False  # konuşma; işlem yok
    after = stored(a)
    assert len(after.decisions) == 1 and after.action_items == []
    before_meeting = json.loads(snapshot)
    assert before_meeting["decisions"] == after.to_dict()["decisions"]


@pytest.mark.parametrize("spoken", ["yok", "yok son tarih yok", "yok, son tarih yok", "belirsiz", "bilmiyorum",
                                    "Mehmet son tarih cuma", "Kimse"])
def test_manager_rejects_non_name_owner(manager, spoken):
    manager.start_meeting()
    manager.add_action("rapor hazırlansın")
    result = manager.set_last_action_owner(spoken)
    assert result.ok is False and manager.meeting.action_items[-1].owner == ""


def test_manager_still_accepts_real_names(manager):
    manager.start_meeting()
    manager.add_action("rapor hazırlansın")
    assert manager.set_last_action_owner("Burak Kaya").ok
    assert manager.meeting.action_items[-1].owner == "Burak Kaya"
    assert manager.set_last_action_owner("Mehmet").ok
    assert manager.meeting.action_items[-1].owner == "Mehmet Öztürk"


@pytest.mark.parametrize("text,expected", [("yok", True), ("sorumlu yok", True), ("Son tarih yok.", True),
                                           ("belli değil", True), ("Mehmet", False), ("yok son tarih yok", False)])
def test_is_none_value(text, expected):
    assert is_none_value(text) is expected


@pytest.mark.parametrize("text,expected", [("Burak Kaya", True), ("Ömer", True), ("yok son tarih yok", False),
                                           ("Mehmet son tarih cuma", False), ("3. kat ekibi", False)])
def test_plausible_person_name(text, expected):
    assert plausible_person_name(text) is expected


# ====================================================================== 2. hitapsız komut kalıpları
@pytest.mark.parametrize("text,flag,proposable", [
    ("Ömer katıldı", True, IntentType.MARK_PRESENT),
    ("Kişi ekle Ahmet Mehmet Dursun", True, None),
    ("sorumlu yok son tarih yok", True, None),
    ("toplantı özeti çıkar", True, None),
    ("toplantıyı bitir", True, None),          # önemli işlem: asla önerilmez
    ("bir sonraki maddeye geçelim", True, IntentType.NEXT_ITEM),
    ("karar verelim artık", False, None),      # kayıt komutu kalıbı ama toplantı konuşması
    ("görev dağılımını sonra yaparız", False, None),
    ("Bakım takvimini tedarikçiyle netleştirmemiz gerekiyor.", False, None),
    ("evet", False, None),
    ("toplantı çok uzadı bence", False, None),
])
def test_classify_unaddressed(text, flag, proposable):
    result = classify_unaddressed(PARSER, text)
    assert result.command_like is flag
    assert (result.intent.type if result.intent else None) == proposable


def test_unaddressed_command_is_flagged_and_proposed_then_marked(started):
    a = started
    r = a.handle_text("Ömer katıldı")
    assert "«Jarvis» duyulmadı" in r.message and a.pending is not None
    assert not any(p.name == "Ömer" for p in a.manager.meeting.participants)  # onaysız çalışmadı
    entry = a.manager.meeting.transcript[-1]
    assert entry.kind == TranscriptKind.COMMAND_LIKE and not entry.is_speech
    a.handle_text("evet")
    assert any(p.name == "Ömer" and p.present for p in a.manager.meeting.participants)
    assert stored(a).transcript[-1].kind == TranscriptKind.EXECUTED  # silinmedi, işaretlendi


def test_unaddressed_command_rejected_stays_flagged(started):
    a = started
    a.handle_text("Ömer katıldı")
    a.handle_text("hayır")
    assert stored(a).transcript[-1].kind == TranscriptKind.COMMAND_LIKE
    assert not any(p.name == "Ömer" for p in a.manager.meeting.participants)


@pytest.mark.parametrize("text", ["Kişi ekle Ahmet Mehmet Dursun", "toplantı özeti çıkar", "toplantıyı bitir",
                                  "sorumlu yok son tarih yok"])
def test_flag_only_patterns_are_silent(started, text):
    r = started.handle_text(text)
    assert r.is_command is False and started.pending is None
    assert stored(started).transcript[-1].kind == TranscriptKind.COMMAND_LIKE
    assert started.manager.meeting.ended_at is None


def test_normal_speech_is_not_flagged(started):
    started.handle_text("Bakım takvimini tedarikçiyle netleştirmemiz gerekiyor.")
    assert stored(started).transcript[-1].kind == ""


def test_prompt_can_be_disabled(manager, settings, minutes, console):
    from jarvis.assistant import JarvisAssistant
    from jarvis.voice.speech_engine import SpeechEngine
    a = JarvisAssistant(manager, PARSER, SpeechEngine(enabled=False, output=console), minutes=minutes,
                        propose_unaddressed=False)
    a.handle_text("Jarvis toplantıyı başlat")
    r = a.handle_text("Ömer katıldı")
    assert r.is_command is False and a.pending is None
    assert a.manager.meeting.transcript[-1].kind == TranscriptKind.COMMAND_LIKE


def test_no_proposal_while_dialog_question_open(started):
    started.handle_text("Jarvis, görev ekle")
    started.handle_text("bir sonraki maddeye geçelim lütfen hemen şimdi arkadaşlar zaman daralıyor")
    assert started.dialog.question is not None and started.pending is None


def test_analysis_ignores_command_like_lines():
    t0 = datetime(2026, 10, 14, 10, 0)
    transcript = [TranscriptEntry(t0, "Ayşe Yılmaz", "Bakım takvimi netleşmeli"),
                  TranscriptEntry(t0, "Ayşe Yılmaz", "görev ekle", kind=TranscriptKind.COMMAND_LIKE),
                  TranscriptEntry(t0, "Ayşe Yılmaz", "Ömer katıldı", kind=TranscriptKind.EXECUTED),
                  TranscriptEntry(t0, "Ayşe Yılmaz", "Mehmet raporu hazırlasın")]
    lines = build_lines(transcript)
    assert [line.id for line in lines] == ["L1", "L4"]  # numaralandırma korunur


def test_pdf_marks_command_lines_and_explains_speaker_metric(started, minutes):
    a = started
    a.handle_text("Jarvis, söz Ayşe'de")
    a.handle_text("Bakım takvimini netleştirmemiz gerekiyor.")
    a.handle_text("toplantı özeti çıkar")
    text = pdf_text(minutes.create_pdf(a.manager.meeting).data["path"])
    assert "[komut gibi duyuldu, işlenmedi] toplantı özeti çıkar" in text.replace("\n", " ")
    assert "Söz süresi" in text and "gerçekten konuştuğu süre değildir" in text.replace("\n", " ")


def test_transcript_entry_roundtrip_and_backward_compat():
    t0 = datetime(2026, 10, 14, 10, 0)
    entry = TranscriptEntry(t0, "Ayşe", "x", kind=TranscriptKind.COMMAND_LIKE, duration_s=2.345)
    again = TranscriptEntry.from_dict(json.loads(json.dumps(entry.to_dict())))
    assert again.kind == TranscriptKind.COMMAND_LIKE and again.duration_s == 2.35
    old = TranscriptEntry.from_dict({"timestamp": "2026-10-14T10:00:00", "speaker": "A", "text": "y"})
    assert old.kind == "" and old.duration_s is None and "kind" not in old.to_dict()
    turn = SpeakerTurn("A", t0, t0 + timedelta(seconds=5), resumed=True)
    assert SpeakerTurn.from_dict(turn.to_dict()).resumed is True
    assert "resumed" not in SpeakerTurn("A", t0).to_dict()


# ====================================================================== 3. söz süresi ve konuşma ölçümü
def test_recovery_does_not_count_downtime(repo, settings, clock):
    manager = MeetingManager(repo, settings, clock=clock)
    manager.start_meeting()
    manager.set_speaker("Mehmet")
    clock.advance(seconds=30)
    manager.add_transcript("Kalite verilerini ekledim.")
    clock.advance(hours=2)  # program kapalı kaldı
    restarted = MeetingManager(repo, settings, clock=clock)
    message = restarted.recover_active_meeting().message
    assert "söz süresine sayılmadı" in message
    assert restarted.meeting.active_speaker == "Mehmet Öztürk"  # söz kişide sürer (mevcut davranış)
    clock.advance(seconds=10)
    stats = {s.name: s for s in SpeakerTracker(restarted.meeting).stats(now=clock())}
    assert stats["Mehmet Öztürk"].total_seconds == 40  # 30 sn + kurtarma sonrası 10 sn; 2 saat sayılmadı
    assert stats["Mehmet Öztürk"].turns == 1  # devam turu yeni söz alma sayılmaz


def test_speech_segments_and_voice_duration(started):
    a = started
    a.handle_text("Jarvis, söz Mehmet'te")
    start = datetime(2026, 10, 14, 10, 1, 0)
    a.handle_text(Utterance("Kalite verilerini rapora ekleyeceğim.", captured_at=start,
                            ended_at=start + timedelta(seconds=4)))
    a.handle_text(Utterance("Ömer katıldı", captured_at=start, ended_at=start + timedelta(seconds=1)))
    entries = a.manager.meeting.transcript
    assert entries[0].duration_s == 4 and entries[0].speaker == "Mehmet Öztürk"
    r = a.handle_text("Jarvis, konuşma süreleri")
    assert "Söz süreleri" in r.message and "gerçek konuşma ölçümü değildir" in r.message
    assert "1 konuşma kaydı" in r.message  # komut kalıbı ("Ömer katıldı") konuşma sayılmadı


# ====================================================================== 4. tanılama ve LLM yapılandırması
def test_trace_records_stages_without_text(started, tmp_path):
    a = started
    path = tmp_path / "trace.jsonl"
    a.trace = PipelineTrace(path, include_text=False, llm_enabled=False)
    a.handle_text("Jarvis, gündemi oku")
    a.handle_text("Jarvis, bütçeyi bir gözden geçirelim mi")
    a.handle_text("Bugün hava çok güzel")
    a.handle_text("Ömer katıldı")
    records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [r["sonuc"] for r in records] == ["islem_yapildi", "anlasilamadi", "transkript", "onay_istendi"]
    assert records[1]["yonlendirici"] == "kapalı" and records[1]["ayristirici"] == "UNKNOWN"
    assert records[2]["hitap"] == "hitapsız"
    raw = path.read_text(encoding="utf-8")
    assert "bütçe" not in raw and "hava" not in raw and "metin" not in records[0]
    report = trace_report(path)
    assert "LLM KAPALIYKEN" in report and "«Komutu anlayamadım»" in report
    assert "bütçe" not in report


def test_trace_text_opt_in(started, tmp_path):
    path = tmp_path / "trace.jsonl"
    started.trace = PipelineTrace(path, include_text=True)
    started.handle_text("Jarvis, gündemi oku")
    assert json.loads(path.read_text(encoding="utf-8"))["metin"] == "Jarvis, gündemi oku"


def test_trace_write_failure_never_breaks_meeting(started, tmp_path):
    blocker = tmp_path / "dosya"
    blocker.write_text("x")
    started.trace = PipelineTrace(blocker / "alt" / "trace.jsonl")  # klasör oluşturulamaz
    assert started.handle_text("Jarvis, gündemi oku").ok


def test_config_report_explains_benchmark_vs_live(tmp_path):
    off = Settings(base_dir=tmp_path, llm_enabled=False, llm_model="qwen3:4b",
                   llm_compare_models=["qwen3.5:4b"], env_status="okundu (x)")
    text = "\n".join(config_report(off))
    assert "LLM_ENABLED=false" in text and "--llm-benchmark bu ayara bakmaz" in text
    on = Settings(base_dir=tmp_path, llm_enabled=True, llm_model="qwen3:4b", llm_compare_models=["qwen3.5:4b"])
    assert any("benchmark modelleri" in line for line in config_report(on))
    same = Settings(base_dir=tmp_path, llm_enabled=True, llm_model="qwen3.5:4b", llm_compare_models=["qwen3.5:4b"])
    assert not any(line.startswith("UYARI") for line in config_report(same))


def test_env_status_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_ENV_FILE", raising=False)
    settings = load_settings(tmp_path / "olmayan.env")
    assert settings.env_status.startswith("bulunamadı")
    assert "SMTP" not in settings.env_status  # değer değil, yalnızca durum


# ====================================================================== 5B. son yanıtı tekrar et
def test_repeat_last_reply_keeps_pending_and_question(started):
    a = started
    r = a.handle_text("Jarvis, son söylediğini tekrar et")
    assert r.intent == IntentType.REPEAT_LAST and "Toplantı başlatıldı" in r.message
    a.handle_text("Jarvis, görev ekle")
    r = a.handle_text("Jarvis, ne dedin")
    assert "Görevin içeriği nedir" in r.message and a.dialog.question is not None
    a.handle_text("raporu hazırlasın")
    a.handle_text("sorumlu yok")
    a.handle_text("tarih yok")
    r = a.handle_text("Jarvis, tekrar eder misin")
    assert "Kaydetmemi onaylıyor musunuz" in r.message and a.pending is not None
    a.handle_text("evet")
    assert len(a.manager.meeting.action_items) == 1


def test_repeat_without_reply(assistant):
    r = assistant.handle_text("Jarvis, ne dedin")
    assert r.ok is False and "Henüz" in r.message


def test_repeat_is_not_meeting_speech_repeat():
    assert PARSER.parse("Jarvis, toplantıdaki son konuşmayı tekrar et").type != IntentType.REPEAT_LAST
    assert PARSER.parse("Jarvis, tutanağı tekrar gönder").type == IntentType.SEND_MINUTES


def test_settings_defaults_documented():
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    for key in ("UNADDRESSED_COMMAND_PROMPT", "PIPELINE_TRACE", "PIPELINE_TRACE_TEXT"):
        assert f"{key}=" in example
