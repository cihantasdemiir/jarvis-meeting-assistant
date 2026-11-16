"""TTS: tek iş parçacığı, Türkçe ses seçimi, yankı koruması, hata ve takılma durumları (SAPI5 taklidi)."""
import io
import threading
import time
import types

from jarvis.voice.speech_engine import SpeechEngine


class FakeVoice:
    def __init__(self, name, vid, languages=()):
        self.name = name
        self.id = vid
        self.languages = list(languages)


class FakeEngine:
    def __init__(self, voices=None, delay=0.0, hang=False, fail_say=False):
        self.props = {}
        self.spoken = []
        self.threads = set()
        self.voices = voices if voices is not None else [
            FakeVoice("Microsoft Zira Desktop", "zira", ["en-US"]),
            FakeVoice("Microsoft Tolga Desktop", r"HKEY\Speech\Voices\TTS_MS_TR-TR_TOLGA_11.0", ["tr-TR"]),
        ]
        self.delay = delay
        self.hang = hang
        self.fail_say = fail_say
        self._pending = None

    def setProperty(self, key, value):  # noqa: N802 (pyttsx3 API)
        self.props[key] = value

    def getProperty(self, key):  # noqa: N802
        return self.voices if key == "voices" else self.props.get(key)

    def say(self, text):
        if self.fail_say:
            raise RuntimeError("SAPI hatası")
        self._pending = text

    def runAndWait(self):  # noqa: N802
        self.threads.add(threading.get_ident())
        if self.hang:
            time.sleep(5)
        time.sleep(self.delay)
        self.spoken.append(self._pending)

    def stop(self):
        pass


def make(engine=None, **kwargs):
    engine = engine or FakeEngine()
    out = io.StringIO()
    speech = SpeechEngine(enabled=True, output=out, engine_factory=lambda: engine, **kwargs)
    return speech, engine, out


def test_turkish_voice_selected():
    speech, engine, _ = make()
    assert speech.enabled and speech.voice_name == "Microsoft Tolga Desktop"
    assert "TOLGA" in engine.props["voice"] and engine.props["rate"] == 165


def test_any_turkish_voice_when_hint_missing():
    voices = [FakeVoice("Microsoft David", "david", ["en-US"]), FakeVoice("Seda", "seda-voice", [b"tr_TR"])]
    speech, _, _ = make(FakeEngine(voices=voices), voice_hint="tolga")
    assert speech.voice_name == "Seda"


def test_no_turkish_voice_warns_but_works():
    speech, engine, _ = make(FakeEngine(voices=[FakeVoice("Microsoft David", "david", ["en-US"])]))
    assert speech.enabled and speech.voice_name is None
    assert "Türkçe ses bulunamadı" in speech.status_message
    speech.say("Merhaba")
    assert engine.spoken == ["Merhaba"]


def test_console_always_printed_and_all_speech_on_one_thread():
    speech, engine, out = make()
    threads = [threading.Thread(target=speech.say, args=(f"Mesaj {i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    speech.say("Son mesaj")
    assert sorted(engine.spoken) == sorted([f"Mesaj {i}" for i in range(5)] + ["Son mesaj"])
    assert len(engine.threads) == 1  # SAPI5/COM için tek iş parçacığı
    assert out.getvalue().count("JARVIS: ") == 6


def test_echo_guard_window():
    speech, _, _ = make(FakeEngine(delay=0.2))
    assert not speech.is_muted(0.1)
    worker = threading.Thread(target=speech.say, args=("Uzun bir cümle",))
    worker.start()
    time.sleep(0.05)
    assert speech.is_speaking and speech.is_muted(0.1)
    worker.join()
    assert speech.is_muted(1.0)  # konuşma bitti ama koruma süresi içinde
    time.sleep(0.15)
    assert not speech.is_muted(0.1)


def test_say_without_wait_returns_immediately():
    speech, engine, _ = make(FakeEngine(delay=0.3))
    started = time.monotonic()
    speech.say("Arka planda", wait=False)
    assert time.monotonic() - started < 0.2
    time.sleep(0.5)
    assert engine.spoken == ["Arka planda"]


def test_init_failure_falls_back_to_console():
    def broken():
        raise RuntimeError("SAPI5 yok")
    out = io.StringIO()
    speech = SpeechEngine(enabled=True, output=out, engine_factory=broken)
    assert not speech.enabled and "TTS başlatılamadı" in speech.status_message
    speech.say("Yine de yazılır")
    assert "Yine de yazılır" in out.getvalue()


def test_missing_pyttsx3_reported(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyttsx3":
            raise ImportError("yok")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    speech = SpeechEngine(enabled=True, output=io.StringIO())
    assert not speech.enabled and "pyttsx3 kurulu değil" in speech.status_message


def test_say_error_does_not_crash():
    speech, _, out = make(FakeEngine(fail_say=True))
    speech.say("Hata olsa da")
    assert "Hata olsa da" in out.getvalue() and not speech.is_speaking


def test_hanging_engine_disables_tts_instead_of_freezing():
    speech, _, out = make(FakeEngine(hang=True), max_utterance_s=0.3)
    started = time.monotonic()
    speech.say("Takılan cümle")
    assert time.monotonic() - started < 2
    assert not speech.enabled and "yanıt vermedi" in speech.status_message
    speech.say("Sonraki cümle")  # artık yalnızca konsol
    assert "Sonraki cümle" in out.getvalue()


def test_disabled_engine_never_mutes():
    speech = SpeechEngine(enabled=False, output=io.StringIO())
    assert not speech.is_muted(10)


def test_pyttsx3_module_used_when_no_factory(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setitem(__import__("sys").modules, "pyttsx3", types.SimpleNamespace(init=lambda: engine))
    speech = SpeechEngine(enabled=True, output=io.StringIO())
    speech.say("Modülden")
    assert engine.spoken == ["Modülden"]
