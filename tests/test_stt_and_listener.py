"""STT (Google taklidi) ve VoiceListener: sıra, internet kesintisi, kapanışta boşaltma."""
import socket
import threading
import types
from datetime import datetime

import pytest

from jarvis.voice.audio_capture import AudioSegment
from jarvis.voice.command_parser import CommandParser
from jarvis.voice.stt import GoogleSpeechToText, SttNetworkError, SttResult, SttUnavailable, create_stt, \
    parse_google_response
from jarvis.voice.voice_listener import Utterance, VoiceListener, choose_transcript
from tests.voice_fakes import FakeSource, FakeStt, network_error, silence, tone, utterances

T0 = datetime(2026, 10, 14, 10, 0, 0)
SEGMENT = AudioSegment(pcm=b"\x00\x00" * 1600, sample_rate=16000, started_at=T0, ended_at=T0)


# ------------------------------------------------------------------ Google yanıtı
def test_parse_google_response():
    response = {"alternative": [{"transcript": "Jarvis gündemi oku", "confidence": 0.93},
                                {"transcript": "carvis gündemi oku"}], "final": True}
    assert parse_google_response(response) == (["Jarvis gündemi oku", "carvis gündemi oku"], 0.93)
    assert parse_google_response([]) == ([], None)  # eski sürümler boş liste döndürür
    assert parse_google_response({"alternative": []}) == ([], None)


def fake_sr_module(behaviour):
    """speech_recognition modülünün taklidi."""
    module = types.SimpleNamespace()

    class UnknownValueError(Exception):
        pass

    class RequestError(Exception):
        pass

    class Recognizer:
        operation_timeout = None

        def recognize_google(self, audio, key=None, language="en-US", show_all=False):
            self.called_with = (language, show_all, key)
            return behaviour(module)

    module.UnknownValueError = UnknownValueError
    module.RequestError = RequestError
    module.Recognizer = Recognizer
    module.AudioData = lambda pcm, rate, width: (pcm, rate, width)
    return module


def test_google_success_uses_turkish_and_alternatives():
    sr = fake_sr_module(lambda m: {"alternative": [{"transcript": "Jarvis sıradaki madde", "confidence": 0.9}]})
    stt = GoogleSpeechToText(timeout_s=5, sr_module=sr)
    result = stt.transcribe(SEGMENT)
    assert result.text == "Jarvis sıradaki madde" and result.confidence == 0.9
    assert stt.recognizer.called_with == ("tr-TR", True, None)
    assert stt.recognizer.operation_timeout == 5


def test_google_unintelligible_returns_none():
    def raise_unknown(m):
        raise m.UnknownValueError()
    assert GoogleSpeechToText(sr_module=fake_sr_module(raise_unknown)).transcribe(SEGMENT) is None
    assert GoogleSpeechToText(sr_module=fake_sr_module(lambda m: [])).transcribe(SEGMENT) is None


@pytest.mark.parametrize("error", ["request", "timeout", "oserror"])
def test_google_network_errors(error):
    def raise_error(m):
        if error == "request":
            raise m.RequestError("recognition connection failed: [Errno 11001] getaddrinfo failed")
        if error == "timeout":
            raise socket.timeout("timed out")
        raise OSError("ağ yok")
    with pytest.raises(SttNetworkError):
        GoogleSpeechToText(sr_module=fake_sr_module(raise_error)).transcribe(SEGMENT)


def test_create_stt_errors():
    with pytest.raises(SttUnavailable, match="Faz 5"):
        create_stt("whisper")
    with pytest.raises(SttUnavailable, match="Bilinmeyen"):
        create_stt("azure")


def test_choose_transcript_prefers_addressed_alternative():
    parser = CommandParser("jarvis", [])
    result = SttResult("carvis gündemi oku", ["carvis gündemi oku", "Jarvis gündemi oku"])
    assert choose_transcript(result, parser.is_addressed) == "Jarvis gündemi oku"
    plain = SttResult("bugün hava güzel", ["bugün hava güzel", "bugün hava güzeldi"])
    assert choose_transcript(plain, parser.is_addressed) == "bugün hava güzel"


def test_utterance_is_a_string_with_metadata():
    u = Utterance("Jarvis gündemi oku", captured_at=T0, confidence=0.8, stt_latency=0.4)
    assert u == "Jarvis gündemi oku" and u.upper().startswith("JARVIS")
    assert u.captured_at == T0 and u.stt_latency == 0.4


# ------------------------------------------------------------------ VoiceListener
def collect(listener: VoiceListener, count: int) -> list:
    items = []
    for _ in range(count):
        item = listener.listen()
        if item is None:
            break
        items.append(item)
    return items


def test_listener_returns_texts_in_order_with_timing():
    texts = ["Jarvis toplantıyı başlat", "Birinci konuşma.", "İkinci konuşma."]
    listener = VoiceListener(FakeSource(utterances(3)), FakeStt(list(texts)))
    got = collect(listener, 3)
    listener.close()
    assert got == texts
    assert all(isinstance(u, Utterance) and u.captured_at and u.ended_at for u in got)
    assert got[0].captured_at < got[1].captured_at < got[2].captured_at
    assert listener.stats.recognized == 3


def test_unintelligible_segments_are_skipped():
    listener = VoiceListener(FakeSource(utterances(3)), FakeStt(["Birinci", None, "Üçüncü"]))
    got = collect(listener, 2)
    listener.close()
    assert got == ["Birinci", "Üçüncü"]
    assert listener.stats.unintelligible == 1


def test_network_outage_keeps_order_saves_audio_and_notifies(tmp_path):
    notes = []
    saved_during_outage = []

    def check_pending(call_index):
        if call_index == 2:  # ikinci başarısız denemede yedek dosya var mı?
            saved_during_outage.extend((tmp_path / "pending").glob("*.wav"))

    stt = FakeStt([network_error(), network_error(), "Birinci", "İkinci"], on_call=check_pending)
    listener = VoiceListener(FakeSource(utterances(2)), stt, notify=notes.append, pending_dir=tmp_path / "pending",
                             initial_retry_s=0.01, max_retry_s=0.02)
    got = collect(listener, 2)
    listener.close()
    assert got == ["Birinci", "İkinci"]  # bölüt atılmadı, sıra korundu
    assert len(saved_during_outage) == 1  # kesinti sırasında ses diske yedeklendi
    assert list((tmp_path / "pending").glob("*.wav")) == []  # işlenince silindi
    assert sum("İnternet bağlantısı yok" in n for n in notes) == 1  # bir kez bildirilir
    assert any("yeniden bağlanıldı" in n for n in notes)
    assert listener.stats.network_errors == 2


def test_muted_listener_ignores_own_voice():
    muted = threading.Event()
    muted.set()
    listener = VoiceListener(FakeSource(utterances(2)), FakeStt(["olmamalı", "olmamalı"]), is_muted=muted.is_set)
    assert listener.listen() is None  # kaynak bitti, hiçbir şey tanınmadı
    listener.close()


def test_close_drains_unfinished_speech():
    gate = threading.Event()
    source = FakeSource(silence(900) + tone(900), block_at_end=gate)
    listener = VoiceListener(source, FakeStt(["Kapanırken söylenen cümle."]))
    listener.start()
    threading.Timer(0.3, gate.set).start()
    listener.close()
    assert listener.drain() == ["Kapanırken söylenen cümle."]


def test_microphone_disconnect_is_announced():
    notes = []
    frames = utterances(2)
    listener = VoiceListener(FakeSource(frames, fail_at={len(frames) // 2}), FakeStt(["Bir", "İki", "Üç"]),
                             notify=notes.append)
    listener.capture.max_backoff_s = 0.05
    collect(listener, 2)
    listener.close()
    assert any("koptu" in n for n in notes)
