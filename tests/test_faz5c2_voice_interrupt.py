"""Faz 5c.2 sesli etkileşim: asistan konuşurken "Jarvis, dur" ve Esc ile sesi kesme.

Sahte mikrofon, sahte STT ve sahte hoparlör kullanılır (gerçek ses aygıtı, Google STT veya Pocket modeli YOK).
Bu testler mantığı, iş parçacığı güvenliğini ve "yankı hiçbir kaydı tetiklemez" kuralını doğrular. Gerçek
hoparlör yankısında algılama başarısını DOĞRULAMAZ (bkz. docs/SESLI_KESME.md).
"""
from __future__ import annotations

import io
import threading
import time

import pytest

from jarvis.assistant import AssistantResponse
from jarvis.voice.audio_capture import AudioCapture
from jarvis.voice.barge_in import BargeInDetector, is_stop_command
from jarvis.voice.command_parser import CommandParser, IntentType
from jarvis.voice.keyboard import ESC, ConsoleLineReader, KeyWatcher
from jarvis.voice.pocket_speech import PocketSpeechEngine
from jarvis.voice.stt import SttResult
from jarvis.voice.unaddressed import classify_unaddressed
from tests.tts_fakes import FakeBackend, FakeOutput
from tests.voice_fakes import FakeSource, FakeStt, silence, tone

PARSER = CommandParser("jarvis", ["carvis", "çarvis"])
STRIP = lambda text: PARSER.split_wake_word(text)[1]  # noqa: E731


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def pocket():
    created = []

    def make(**kwargs):
        engine = PocketSpeechEngine(kwargs.pop("backend", FakeBackend(chunks=40)), FakeOutput(speed=1.0),
                                    console=io.StringIO(), prebuffer_ms=100, **kwargs)
        created.append(engine)
        return engine
    yield make
    for engine in created:
        engine.stop()


class RecordingSpeech:
    """Asistan testleri için: söylenenleri ve iptalleri kaydeder."""

    def __init__(self):
        self.said, self.notes, self.cancels = [], [], 0
        self.is_speaking = False
        self.enabled = False

    def say(self, text, wait=True):
        self.said.append(text)

    def note(self, text):
        self.notes.append(text)

    def cancel_all(self):
        self.cancels += 1
        self.is_speaking = False

    def is_muted(self, guard_s=0.7):
        return False


@pytest.fixture
def a(assistant):
    assistant.speech = RecordingSpeech()
    assistant.handle_text("Jarvis toplantıyı başlat")
    return assistant


# ====================================================================== komut
@pytest.mark.parametrize("text", ["Jarvis, dur.", "Jarvis sus", "Jarvis, yeter", "Jarvis dinle", "Jarvis, sus artık",
                                  "Carvis dur"])
def test_stop_words_parse(text):
    assert PARSER.parse(text).type == IntentType.STOP_SPEECH


def test_stop_is_not_exit_and_not_unaddressed():
    assert PARSER.parse("Jarvis çık").type == IntentType.EXIT
    assert PARSER.parse("dur", require_wake=True) is None
    assert classify_unaddressed(PARSER, "dur").command_like is False  # toplantıda "dur" demek konuşmadır


def test_stop_cancels_speech_silently_and_keeps_state(a):
    a.handle_text("Jarvis, görev ekle")  # bekleyen soru
    question = a.dialog.question
    r = a.handle_text("Jarvis, dur")
    assert r.intent == IntentType.STOP_SPEECH and r.speak is False and a.speech.cancels == 1
    assert a.dialog.question is question  # soru korunur
    a.respond(r)
    assert a.speech.notes == ["Sesli yanıt durduruldu; dinliyorum."] and a.speech.said == []
    a.handle_text("raporu hazırlasın")
    a.handle_text("sorumlu yok")
    a.handle_text("tarih yok")
    pending = a.pending
    a.handle_text("Jarvis, sus")
    assert a.pending is pending  # bekleyen onay korunur
    a.handle_text("evet")
    assert len(a.manager.meeting.action_items) == 1


def test_repeat_after_stop_repeats_interrupted_answer(a):
    a.handle_text("Jarvis, gündemi oku")
    a.handle_text("Jarvis, dur")
    r = a.handle_text("Jarvis, son söylediğini tekrar et")
    assert "Üretim planlaması" in r.message


def test_escape_only_when_speaking(a):
    a.escape_pressed()
    assert a.speech.cancels == 0
    a.speech.is_speaking = True
    a.escape_pressed()
    assert a.speech.cancels == 1 and a.speech.notes[-1].startswith("(Esc")


# ====================================================================== Pocket oynatma kesme
def test_long_reply_stops_quickly_and_next_reply_plays(pocket):
    engine = pocket()
    assert engine.wait_ready(5)
    job = engine.say("Birinci gündem maddesi üretim planlaması, ikinci madde kalite raporu, üçüncü madde bütçe.")
    assert wait_until(lambda: engine.is_playing)
    assert engine.current_text  # yankı denetimi için çalan metin bilinir
    started = time.monotonic()
    engine.cancel_all()
    assert job.done.wait(1.0) and time.monotonic() - started < 0.5
    assert job.status == "cancelled" and not engine.is_playing and engine.current_text == ""
    second = engine.say("İkinci maddeye geçildi.")
    assert wait_until(lambda: second.done.is_set(), 10) and second.status in ("done", "cancelled")
    assert engine.enabled


def test_repeated_stop_and_speak_no_deadlock(pocket):
    engine = pocket()
    assert engine.wait_ready(5)
    for index in range(5):
        job = engine.say(f"Uzun yanıt numara {index}, bu cümle kesilecek.")
        assert wait_until(lambda: engine.is_playing or job.done.is_set())
        engine.cancel_all()
        assert job.done.wait(1.0)
    final = engine.say("Son yanıt.")
    assert final.done.wait(10)
    engine.stop()
    assert not any(t.is_alive() for t in (engine._synth, engine._player))


def test_cancel_from_other_thread_while_saying(pocket):
    engine = pocket()
    assert engine.wait_ready(5)
    errors = []

    def spam():
        try:
            for _ in range(20):
                engine.cancel_all()
                time.sleep(0.005)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
    thread = threading.Thread(target=spam)
    thread.start()
    jobs = [engine.say(f"yanıt {i}") for i in range(5)]
    thread.join(5)
    assert not errors and all(job.done.wait(5) for job in jobs if job is not None)


# ====================================================================== kesme algılayıcısı (karar)
def detector(on_stop=None):
    calls = []
    det = BargeInDetector(FakeStt([]), on_stop or calls.append, PARSER.is_addressed, STRIP)
    return det, calls


@pytest.mark.parametrize("heard,stops", [
    ("Jarvis dur", True), ("jarvis sus", True), ("Jarvis, yeter.", True), ("Carvis dinle", True),
    ("Jarvis evet", False), ("evet", False), ("onaylıyorum", False), ("Jarvis, evet onaylıyorum", False),
    ("dur", False),  # hitap zorunlu
    ("Jarvis dur bir saniye bakalım şimdi", False),  # uzun cümle: kesme değil
    ("Jarvis toplantıyı bitir", False), ("Jarvis gündemi oku", False),
])
def test_only_stop_command_has_any_effect(heard, stops):
    det, calls = detector()
    assert det.evaluate([heard], playing_text="Kaydetmemi onaylıyor musunuz? Onaylamak için evet deyin.") is stops
    assert len(calls) == (1 if stops else 0)


def test_assistants_own_sentence_is_echo():
    det, calls = detector()
    playing = "Asistan konuşurken «Jarvis, dur» diyebilirsiniz."
    assert det.evaluate(["Jarvis dur"], playing) is False and calls == [] and det.stats.echo_rejected == 1
    assert det.evaluate(["Carvis dur"], playing) is False  # STT hitabı farklı duysa bile


def test_is_stop_command_helper():
    assert is_stop_command("Jarvis, dur", PARSER.is_addressed, STRIP)
    assert not is_stop_command("Jarvis'in dur dediği", PARSER.is_addressed, STRIP)


# ====================================================================== tek mikrofon okuyucu + yankı
def _capture_with_window(stt_outputs, user_amplitude=7000):
    """Kalibrasyon → [susturma: yankı + kullanıcı + yankı] → sessizlik. Susturma, çerçeve sayısıyla yönetilir."""
    lead = silence(900)
    echo1, user, echo2 = tone(450, 400), tone(600, user_amplitude, 330), tone(900, 400)
    frames = lead + echo1 + user + echo2 + silence(600)
    muted_range = range(len(lead), len(lead) + len(echo1) + len(user) + len(echo2))
    stops = []
    stt = FakeStt(list(stt_outputs))
    det = BargeInDetector(stt, stops.append, PARSER.is_addressed, STRIP, current_text=lambda: "Gündem uzun.")
    det.start()
    segments = []
    capture = AudioCapture(FakeSource(frames), on_segment=segments.append, barge_in=det)
    capture.is_muted = lambda: capture.frames_read - 1 in muted_range
    capture.run()  # aynı iş parçacığında: mikrofonu yalnızca AudioCapture okur
    wait_until(lambda: stt.calls >= 1, 3)
    time.sleep(0.05)
    det.close()
    return stops, segments, det, stt


def test_voice_stop_during_playback():
    stops, segments, det, stt = _capture_with_window(["Jarvis dur"])
    assert stops == ["Jarvis dur"] and det.stats.windows == 1 and stt.calls == 1
    assert segments == []  # susturma penceresindeki ses komut/transkript akışına GİRMEDİ


def test_echo_yes_during_playback_never_reaches_assistant():
    stops, segments, det, _ = _capture_with_window(["Jarvis evet onaylıyorum"])
    assert stops == [] and segments == [] and det.stats.other_rejected == 1


def test_quiet_echo_alone_sends_nothing_to_stt():
    stops, segments, det, stt = _capture_with_window(["Jarvis dur"], user_amplitude=400)
    assert stt.calls == 0 and stops == [] and segments == []


def test_detector_failure_never_stops_microphone_loop():
    class Broken:
        def begin_window(self, rate):
            raise RuntimeError("bozuk")
        feed = end_window = begin_window
    frames = silence(900) + tone(300, 400) + silence(900) + tone(600, 7000) + silence(1200)
    segments = []
    capture = AudioCapture(FakeSource(frames), on_segment=segments.append, barge_in=Broken())
    muted = range(30, 40)
    capture.is_muted = lambda: capture.frames_read - 1 in muted
    capture.run()
    assert len(segments) == 1  # susturma sonrası normal konuşma yine tanındı


def test_busy_recognizer_drops_extra_segments_and_closes_cleanly():
    release = threading.Event()

    class SlowStt:
        calls = 0

        def transcribe(self, segment):
            SlowStt.calls += 1
            release.wait(3)
            return SttResult(text="Jarvis dur", alternatives=["Jarvis dur"], confidence=0.9, engine="t",
                             latency_s=1.0)
    det = BargeInDetector(SlowStt(), lambda _: None, PARSER.is_addressed, STRIP)
    det.start()
    det.begin_window(16000)
    from jarvis.voice.audio_capture import AudioSegment
    from datetime import datetime
    segment = AudioSegment(pcm=b"\x00\x00" * 1600, sample_rate=16000, started_at=datetime.now(),
                           ended_at=datetime.now())
    for _ in range(4):
        det._submit(segment)
    assert det.stats.skipped_busy >= 1
    started = time.monotonic()
    release.set()
    det.close(timeout=2)
    assert time.monotonic() - started < 3


# ====================================================================== klavye
def keys(sequence):
    queue_ = list(sequence)
    return (lambda: bool(queue_)), (lambda: queue_.pop(0))


def test_line_reader_reads_turkish_and_esc_keeps_line():
    escapes = []
    kbhit, getwch = keys(list("Jarvis, gündemi ") + [ESC] + list("oku") + ["\r"])
    out = io.StringIO()
    reader = ConsoleLineReader(lambda: escapes.append(1), kbhit=kbhit, getwch=getwch, output=out,
                               sleep=lambda s: None)
    assert reader("Siz > ") == "Jarvis, gündemi oku" and escapes == [1]
    assert out.getvalue().startswith("Siz > ")


def test_line_reader_backspace_special_keys_and_eof():
    kbhit, getwch = keys(list("abc") + ["\x08", "\xe0", "K", "d", "\r"])
    assert ConsoleLineReader(lambda: None, kbhit=kbhit, getwch=getwch, output=io.StringIO(),
                             sleep=lambda s: None)() == "abd"
    kbhit, getwch = keys(["\x1a"])
    with pytest.raises(EOFError):
        ConsoleLineReader(lambda: None, kbhit=kbhit, getwch=getwch, output=io.StringIO(), sleep=lambda s: None)()


def test_line_reader_ctrl_c_still_interrupts():
    kbhit, getwch = keys(["a", "\x03"])
    with pytest.raises(KeyboardInterrupt):
        ConsoleLineReader(lambda: None, kbhit=kbhit, getwch=getwch, output=io.StringIO(), sleep=lambda s: None)()

    def interrupted(_s):
        raise KeyboardInterrupt  # Windows: Ctrl+C sinyali bekleme sırasında gelir
    with pytest.raises(KeyboardInterrupt):
        ConsoleLineReader(lambda: None, kbhit=lambda: False, getwch=lambda: "", output=io.StringIO(),
                          sleep=interrupted)()


def test_key_watcher_esc_and_clean_stop():
    pressed = threading.Event()
    sequence = ["x", "\x00", ";", ESC]
    watcher = KeyWatcher(pressed.set, kbhit=lambda: bool(sequence), getwch=lambda: sequence.pop(0), poll_s=0.01)
    watcher.start()
    assert pressed.wait(2)
    watcher.stop()
    assert not watcher.is_alive()


def test_text_mode_esc_while_speaking_then_command(a):
    a.speech.is_speaking = True
    kbhit, getwch = keys([ESC] + list("Jarvis, sıradaki madde") + ["\r"])
    line = ConsoleLineReader(a.escape_pressed, kbhit=kbhit, getwch=getwch, output=io.StringIO(),
                             sleep=lambda s: None)()
    assert a.speech.cancels == 1
    assert a.handle_text(line).intent == IntentType.NEXT_ITEM


def test_response_speak_flag_default():
    assert AssistantResponse("x").speak is True


def test_voice_listener_lifecycle_with_detector():
    from jarvis.voice.voice_listener import VoiceListener
    det = BargeInDetector(FakeStt([]), lambda _: None, PARSER.is_addressed, STRIP)
    source = FakeSource(silence(900) + tone(600, 6000) + silence(1200))
    listener = VoiceListener(source, FakeStt(["Jarvis gündemi oku"]), barge_in=det)
    assert listener.listen() == "Jarvis gündemi oku"
    started = time.monotonic()
    listener.close()
    assert time.monotonic() - started < 5 and not det._worker.is_alive()


def test_detector_only_built_for_precise_playback_engine(settings, monkeypatch):
    from jarvis.voice import voice_listener as vl
    from jarvis.voice.speech_engine import SpeechEngine

    class Src:
        name = "x"
        sample_rate = 16000

        def __init__(self, device_index=None):
            pass

        def open(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr("jarvis.voice.audio_capture.PyAudioSource", Src)
    monkeypatch.setattr("jarvis.voice.stt.create_stt", lambda *a, **k: FakeStt([]))
    listener, error = vl.build_voice_listener(settings, SpeechEngine(enabled=False, output=io.StringIO()), PARSER)
    assert error is None and listener.barge_in is None  # pyttsx3/sessiz motor: oynatma anı bilinmez
