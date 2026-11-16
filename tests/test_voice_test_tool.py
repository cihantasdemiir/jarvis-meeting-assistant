"""Doğruluk ölçüm aracı: değerlendirme kuralları, rapor ve simüle edilmiş oturum.

Bu testler ölçüm ARACININ doğru hesapladığını doğrular; gerçek mikrofon doğruluğunu ölçmez.
"""
from jarvis.voice.audio_capture import AudioSegment
from jarvis.voice.command_parser import CommandParser
from jarvis.voice.voice_test import (
    ItemResult,
    VoiceTestItem,
    evaluate,
    load_test_set,
    run_live_test,
    run_replay_test,
    suggested_aliases,
    summarize,
    write_report,
)
from tests.voice_fakes import FakeStt

PARSER = CommandParser("jarvis", ["jarvıs", "carvis"])


def test_test_set_meets_pdf_requirement():
    items = load_test_set()
    assert sum(i.is_command for i in items) >= 20  # PDF Hafta 2: en az 20 komut
    assert sum(not i.is_command for i in items) >= 3  # yanlış tetikleme ölçümü


def test_test_set_is_self_consistent():
    """Metnin kendisi doğru ayrıştırılmalı; ölçülen hata yalnızca ses tanımadan gelmeli."""
    for item in load_test_set():
        assert evaluate(item, item.say, PARSER).correct, item.say


def test_evaluate_rules():
    speaker = VoiceTestItem("Jarvis, söz Ayşe'de", ["SET_SPEAKER"], {"speaker": "ayse"})
    assert evaluate(speaker, "jarvis söz ayşede", PARSER).correct
    assert evaluate(speaker, "Jarvis söz Mehmet'te", PARSER).reason.startswith("'speaker'")
    wrong_wake = evaluate(speaker, "Davis söz Ayşe'de", PARSER)
    assert not wrong_wake.addressed and "Davis" in wrong_wake.reason
    assert evaluate(speaker, None, PARSER).reason == "ses anlaşılamadı"
    speech = VoiceTestItem("Bugün hava güzel", None)
    assert evaluate(speech, "bugün hava güzel", PARSER).correct
    assert "yanlış tetikleme" in evaluate(speech, "Jarvis gündemi oku", PARSER).reason


def result(number, item, transcript, latency=0.5):
    end = None if latency is None else latency + 0.2
    return ItemResult(number, item, transcript, [], evaluate(item, transcript, PARSER), latency, end)


def test_summary_and_report(tmp_path):
    agenda = VoiceTestItem("Jarvis gündemi oku", ["READ_AGENDA"])
    nxt = VoiceTestItem("Jarvis sıradaki madde", ["NEXT_ITEM"])
    speech = VoiceTestItem("Normal konuşma", None)
    results = [result(1, agenda, "Jarvis gündemi oku"), result(2, nxt, "Davis sıradaki madde"),
               result(3, speech, "normal konuşma"), result(4, agenda, None, latency=None)]
    summary = summarize(results, skipped=1)
    assert (summary.correct_commands, summary.total_commands) == (1, 3)
    assert round(summary.command_accuracy, 1) == 33.3
    assert summary.false_triggers == 0 and summary.unintelligible == 1 and summary.skipped == 1
    assert suggested_aliases(results, PARSER) == ["davis"]
    md, csv_path, _ = write_report(results, 1, tmp_path, {"device": "Test mikrofonu", "engine": "test"}, PARSER)
    text = md.read_text(encoding="utf-8")
    assert "%33.3" in text and "KALDI" in text and "Test mikrofonu" in text and "`davis`" in text
    assert csv_path.read_text(encoding="utf-8-sig").startswith("No;Söylenen;STT çıktısı")


class PromptedSource:
    """Yalnızca kullanıcı Enter'a bastığında konuşma üreten sahte mikrofon (gerçek kullanımı taklit eder)."""

    def __init__(self):
        import threading

        self.sample_rate = 16000
        self.name = "Simüle mikrofon"
        self.pending: list[bytes] = []
        self.lock = threading.Lock()

    def speak(self):
        from tests.voice_fakes import silence, tone

        with self.lock:
            self.pending += tone(600) + silence(1000)

    def open(self):
        pass

    def read(self):
        import time

        from tests.voice_fakes import silence

        time.sleep(0.001)
        with self.lock:
            if self.pending:
                return self.pending.pop(0)
        return silence(30)[0]

    def close(self):
        pass


def test_live_session_with_simulated_microphone(tmp_path):
    items = [VoiceTestItem("Jarvis gündemi oku", ["READ_AGENDA"]), VoiceTestItem("Jarvis süre durumu", ["TIME_STATUS"]),
             VoiceTestItem("Atlanacak", ["HELP"]), VoiceTestItem("Normal konuşma", None)]
    source = PromptedSource()
    answers = iter(["", "", "a", ""])

    def user(prompt):
        answer = next(answers)
        if answer == "":
            source.speak()  # kullanıcı Enter'a basıp konuşuyor
        return answer

    stt = FakeStt(["Davis gündemi oku", "Jarvis süre durumu", "normal konuşma"])
    printed = []
    md, _, summary = run_live_test(source, stt, PARSER, tmp_path, save_audio=True, items=items,
                                   input_func=user, print_func=printed.append)
    assert (summary.correct_commands, summary.total_commands, summary.skipped) == (1, 2, 1)
    assert summary.false_triggers == 0
    report = md.read_text(encoding="utf-8")
    assert "Davis gündemi oku" in report and "gerçek mikrofon" in report
    manifest = list(tmp_path.glob("ses_*/manifest.json"))
    assert len(manifest) == 1 and len(list(manifest[0].parent.glob("*.wav"))) == 3


def test_replay_from_saved_audio(tmp_path):
    from datetime import datetime

    audio_dir = tmp_path / "ses"
    t = datetime(2026, 10, 14, 10)
    for number in (1, 2):
        AudioSegment(pcm=b"\x10\x00" * 16000, sample_rate=16000, started_at=t, ended_at=t).save_wav(
            audio_dir / f"{number:02d}.wav")
    (audio_dir / "manifest.json").write_text('{"items": [{"number": 1, "file": "01.wav"}, '
                                             '{"number": 2, "file": "02.wav"}]}', encoding="utf-8")
    items = [VoiceTestItem("Jarvis gündemi oku", ["READ_AGENDA"]), VoiceTestItem("Jarvis süre durumu", ["TIME_STATUS"])]
    md, _, summary = run_replay_test(audio_dir, FakeStt(["Jarvis gündemi oku", "carvis süre durumu"]), PARSER,
                                     tmp_path / "rapor", items=items)
    assert summary.command_accuracy == 100.0
    assert "kayıtlı ses dosyalarından" in md.read_text(encoding="utf-8")
