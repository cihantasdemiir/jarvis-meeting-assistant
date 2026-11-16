"""Sesli giriş uçtan uca: mikrofon simülasyonu → bölütleme → STT taklidi → assistant → SQLite."""
import threading
from datetime import datetime

from jarvis.voice.listener import ScriptedListener
from jarvis.voice.voice_listener import Utterance, VoiceListener
from tests.voice_fakes import FakeSource, FakeStt, silence, tone, utterances


def voice_listener(texts: list) -> VoiceListener:
    return VoiceListener(FakeSource(utterances(len(texts))), FakeStt(list(texts)))


def test_t03_voice_path_three_sentences_for_ayse(assistant, repo):
    """T-03 (mikrofon simülasyonu): "söz Ayşe'de" + 3 konuşma → transkriptte 3 satır Ayşe Yılmaz."""
    texts = ["Jarvis toplantıyı başlat", "Jarvis söz Ayşe'de", "Birinci cümle.", "İkinci cümle.", "Üçüncü cümle.",
             "Jarvis çık"]
    assistant.run(voice_listener(texts), monitor_interval=None)
    meeting = repo.get_active()
    assert [(e.speaker, e.text) for e in meeting.transcript] == [
        ("Ayşe Yılmaz", "Birinci cümle."), ("Ayşe Yılmaz", "İkinci cümle."), ("Ayşe Yılmaz", "Üçüncü cümle.")]


def test_transcript_uses_spoken_time_not_recognition_time(assistant, repo):
    assistant.handle_text("Jarvis toplantıyı başlat")
    spoken = datetime(2026, 10, 14, 10, 7, 33)
    assistant.handle_text(Utterance("Gecikmeli tanınan konuşma.", captured_at=spoken))
    assert repo.get_active().transcript[0].timestamp == spoken


def test_embedded_command_is_split(assistant, repo):
    assistant.handle_text("Jarvis toplantıyı başlat")
    response = assistant.handle_text("Bu konuyu böyle kapatalım Jarvis sıradaki madde")
    assert "Sıradaki" in response.message
    meeting = repo.get_active()
    assert [e.text for e in meeting.transcript] == ["Bu konuyu böyle kapatalım"]
    assert meeting.current_item.title == "Kalite raporu"


def test_embedded_unknown_command_kept_as_speech(assistant, repo):
    assistant.handle_text("Jarvis toplantıyı başlat")
    response = assistant.handle_text("Bunu jarvis e de sorduk aslında")
    assert response.message == ""
    assert [e.text for e in repo.get_active().transcript] == ["Bunu jarvis e de sorduk aslında"]


def test_drain_on_ctrl_c_keeps_speech_but_not_commands(assistant, repo, console):
    class DrainingSource(ScriptedListener):
        def __init__(self):
            super().__init__(["Jarvis toplantıyı başlat"])

        def listen(self):
            line = super().listen()
            if line is None:
                raise KeyboardInterrupt
            return line

        def drain(self):
            return [Utterance("Kapanırken söylenen son cümle."), Utterance("Jarvis toplantıyı bitir")]

    assistant.run(DrainingSource(), monitor_interval=None)
    meeting = repo.get_active()
    assert meeting is not None  # kapanışta gelen "bitir" komutu çalıştırılmadı
    assert [e.text for e in meeting.transcript] == ["Kapanırken söylenen son cümle."]
    assert "Ara tutanak oluşturuldu" in console.getvalue()  # drain sonrası ara tutanak


def test_ctrl_c_during_live_listening_flushes_audio(assistant, repo):
    gate = threading.Event()
    texts = ["Jarvis toplantıyı başlat", "Yarıda kalan cümle."]
    source = FakeSource(silence(900) + tone(600) + silence(1000) + tone(900), block_at_end=gate)
    listener = VoiceListener(source, FakeStt(list(texts)))

    class InterruptAfterFirst:
        def __init__(self):
            self.calls = 0

        def listen(self):
            self.calls += 1
            if self.calls == 1:
                return listener.listen()
            gate.set()
            raise KeyboardInterrupt

        def close(self):
            listener.close()

        def drain(self):
            return listener.drain()

    assistant.run(InterruptAfterFirst(), monitor_interval=None)
    assert [e.text for e in repo.get_active().transcript] == ["Yarıda kalan cümle."]
