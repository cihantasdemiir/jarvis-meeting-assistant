"""Faz 5b: Pocket TTS Turkish entegrasyonu — hızlı birim testleri (gerçek model, torch, ses aygıtı YOK).

Sahte model ve sahte hoparlör kullanılır (`tests/tts_fakes.py`). Bu testler kuyruk, iptal, yankı koruması,
hata ve yapılandırma mantığını doğrular; gerçek Türkçe sesin doğallığını veya hızını DOĞRULAMAZ.
Gerçek motorla isteğe bağlı duman testi: `tests/test_tts_pocket_smoke.py`.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from config.settings import Settings, load_settings
from jarvis.assistant import JarvisAssistant
from jarvis.voice import speech_text
from jarvis.voice.audio_capture import AudioCapture
from jarvis.voice.command_parser import CommandParser
from jarvis.voice.pocket_backend import (
    DOWNLOAD_HINT,
    PocketTurkishBackend,
    TtsModelMissing,
    TtsUnavailable,
    check_model_folder,
    resolve_model_folder,
    to_pcm16,
)
from jarvis.voice.pocket_speech import PocketSpeechEngine
from jarvis.voice.speech_engine import SpeechEngine
from jarvis.voice.speech_text import MORE_ON_SCREEN, to_speech_text
from jarvis.voice.tts_bench import measure_generation, run_tts_benchmark
from jarvis.voice.tts_factory import create_speech_engine, shutdown_speech
from tests.tts_fakes import FakeBackend, FakeOutput, marker, missing_model
from tests.voice_fakes import FakeSource, silence, tone

POCKET_INSTALLED = importlib.util.find_spec("pocket_tts_turkish") is not None


def make(backend=None, output=None, **kwargs):
    console = io.StringIO()
    engine = PocketSpeechEngine(backend or FakeBackend(), output if output is not None else FakeOutput(),
                                console=console, prebuffer_ms=kwargs.pop("prebuffer_ms", 100), **kwargs)
    return engine, console


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def engines():
    created = []

    def factory(*args, **kwargs):
        engine, console = make(*args, **kwargs)
        created.append(engine)
        return engine, console
    yield factory
    for engine in created:
        engine.stop()


# ====================================================================== ayarlar
def test_defaults_are_pocket_male_1_cpu():
    s = Settings()
    assert (s.tts_engine, s.tts_voice, s.tts_device) == ("pocket", "male_1", "cpu")
    assert s.tts_model_dir is None and s.tts_echo_guard_ms == 700


def test_settings_read_and_validate(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ENV_FILE", "none")
    monkeypatch.setenv("TTS_ENGINE", "PyTtsx3")
    monkeypatch.setenv("TTS_VOICE", "male_2")
    monkeypatch.setenv("TTS_DEVICE", "cuda")
    monkeypatch.setenv("TTS_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("TTS_PREBUFFER_MS", "99999")
    monkeypatch.setenv("TTS_MAX_QUEUE", "0")
    monkeypatch.setenv("TTS_OUTPUT_DEVICE", "3")
    s = load_settings()
    assert (s.tts_engine, s.tts_voice, s.tts_device, s.tts_model_dir) == ("pyttsx3", "male_2", "cuda", tmp_path)
    assert s.tts_prebuffer_ms == 2000 and s.tts_max_queue == 1 and s.tts_output_device == 3
    assert s.config_warnings == []


def test_invalid_settings_fall_back_with_warning(monkeypatch):
    monkeypatch.setenv("JARVIS_ENV_FILE", "none")
    monkeypatch.setenv("TTS_ENGINE", "elevenlabs")
    monkeypatch.setenv("TTS_DEVICE", "gpu")
    monkeypatch.setenv("TTS_OUTPUT_DEVICE", "hoparlör")
    s = load_settings()
    assert (s.tts_engine, s.tts_device, s.tts_output_device) == ("pocket", "cpu", None)
    assert len(s.config_warnings) == 3 and any("TTS_ENGINE" in w for w in s.config_warnings)


def test_env_example_documents_tts_settings():
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for key in ("TTS_ENGINE=pocket", "TTS_VOICE=male_1", "TTS_DEVICE=cpu", "TTS_MODEL_DIR=", "TTS_ECHO_GUARD_MS"):
        assert key in text


# ====================================================================== fabrika: açık/kapalı
def _forbidden(_settings):
    raise AssertionError("--no-tts iken ses motoru oluşturulmamalı")


def test_no_tts_never_creates_or_loads_model():
    before = set(sys.modules)
    speech = create_speech_engine(Settings(), enabled=False, backend_factory=_forbidden, output_factory=_forbidden)
    assert isinstance(speech, SpeechEngine) and not speech.enabled
    new = set(sys.modules) - before
    assert not any(name.startswith(("pocket_tts", "torch", "pyaudio")) for name in new)


def test_engine_none_is_console_only(capsys):
    speech = create_speech_engine(Settings().with_overrides(tts_engine="none"), enabled=True,
                                  backend_factory=_forbidden, output_factory=_forbidden)
    assert not speech.enabled and "TTS_ENGINE=none" in speech.status_message


def test_pyttsx3_only_when_explicitly_selected():
    speech = create_speech_engine(Settings().with_overrides(tts_engine="pyttsx3"), enabled=True,
                                  backend_factory=_forbidden, output_factory=_forbidden)
    try:
        assert type(speech) is SpeechEngine
    finally:
        speech.stop()


def test_pocket_selected_by_default_and_toggles():
    backend = FakeBackend()
    speech = create_speech_engine(Settings(), enabled=True, backend_factory=lambda s: backend,
                                  output_factory=lambda s: FakeOutput(), console=io.StringIO())
    try:
        assert isinstance(speech, PocketSpeechEngine) and speech.can_speak and backend.load_calls == 1
        off = create_speech_engine(Settings(), enabled=False, backend_factory=_forbidden)
        assert not off.enabled
    finally:
        speech.stop()


def test_pocket_failure_never_falls_back_to_pyttsx3():
    speech = create_speech_engine(Settings(), enabled=True, backend_factory=lambda s: FakeBackend(
        load_error=missing_model()), output_factory=lambda s: FakeOutput(), console=io.StringIO())
    try:
        assert isinstance(speech, PocketSpeechEngine) and not speech.enabled
        assert "konsol" in speech.status_message
    finally:
        speech.stop()


# ====================================================================== yükleme
def test_model_loaded_once_with_warm_up_and_male_1(engines):
    backend = FakeBackend()
    engine, _ = engines(backend)
    for text in ("Bir.", "İki.", "Üç."):
        engine.say(text)
    assert engine.wait_idle(5)
    assert backend.load_calls == 1
    assert backend.requests[0] == ("Hazırım.", "male_1")  # ısınma
    assert {voice for _, voice in backend.requests} == {"male_1"}
    assert engine.voice_name and "male_1" in engine.voice_name and engine.load_seconds is not None


@pytest.mark.parametrize("error,needle", [
    (missing_model(), "--tts-download"),
    (TtsUnavailable("Pocket TTS Turkish kurulu değil. pip install -r requirements-tts.txt"), "requirements-tts"),
    (RuntimeError("CUDA çöktü"), "CUDA çöktü"),
])
def test_load_failure_keeps_console(engines, error, needle):
    engine, console = engines(FakeBackend(load_error=error))
    assert not engine.enabled and needle in engine.status_message and "konsol" in engine.status_message
    assert engine.say("Toplantı başlatıldı.") is None
    assert "JARVIS: Toplantı başlatıldı." in console.getvalue()
    assert not engine.is_muted(10)


def test_missing_voice_is_not_silently_replaced(engines):
    backend = FakeBackend(voices=("female_1", "male_2"))
    engine, _ = engines(backend)
    assert not engine.enabled and "male_1" in engine.status_message and "female_1" in engine.status_message
    assert backend.requests == []


def test_slow_load_does_not_block_forever(engines):
    backend = FakeBackend(load_delay=0.5)
    engine, console = engines(backend, load_timeout_s=0.05)
    assert "yükleniyor" in engine.status_message and engine.say("Erken yanıt.") is None
    assert "Erken yanıt." in console.getvalue()
    assert engine.wait_ready(5) and engine.can_speak


# ====================================================================== çevrimdışı model çözümleme
def _model_dir(tmp_path, complete=True):
    for name in ("config.yaml", "model.safetensors", "tokenizer.model"):
        (tmp_path / name).write_text("x")
    (tmp_path / "voices").mkdir()
    if complete:
        (tmp_path / "voices" / "male_1.wav").write_bytes(b"RIFF")
    return tmp_path


def test_model_dir_used_directly(tmp_path):
    folder = _model_dir(tmp_path)
    assert resolve_model_folder(folder, "repo", snapshot_download=_forbidden) == folder


def test_incomplete_or_missing_model_dir(tmp_path):
    with pytest.raises(TtsModelMissing, match="voices"):
        check_model_folder(_model_dir(tmp_path, complete=False))
    with pytest.raises(TtsModelMissing, match="--tts-download"):
        resolve_model_folder(tmp_path / "yok", "repo")


def test_cache_lookup_is_local_only(tmp_path):
    calls = []

    def fake_snapshot(**kwargs):
        calls.append(kwargs)
        return str(_model_dir(tmp_path))
    assert resolve_model_folder(None, "wite-tech/pocket-tts-turkish", snapshot_download=fake_snapshot) == tmp_path
    assert calls[0]["local_files_only"] is True and calls[0]["repo_id"] == "wite-tech/pocket-tts-turkish"


def test_no_internet_and_no_cache_gives_clear_error():
    def offline(**kwargs):
        assert kwargs["local_files_only"] is True  # internete çıkma denemesi yapılmaz
        raise OSError("LocalEntryNotFoundError: cannot find the requested files in the local cache")
    with pytest.raises(TtsModelMissing) as info:
        resolve_model_folder(None, "wite-tech/pocket-tts-turkish", snapshot_download=offline)
    assert DOWNLOAD_HINT in str(info.value)


@pytest.mark.skipif(POCKET_INSTALLED, reason="paket kurulu ortamda gerçek yükleme yapılmaz")
def test_backend_without_package_is_clear_and_offline(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    with pytest.raises(TtsUnavailable, match="requirements-tts"):
        PocketTurkishBackend().load()
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_pcm16_conversion_bytes_passthrough():
    assert to_pcm16(b"\x01\x02") == b"\x01\x02"
    np = pytest.importorskip("numpy")
    pcm = to_pcm16(np.array([0.0, 1.0, -1.0, 2.0], dtype=np.float32), volume=0.5)
    assert len(pcm) == 8


# ====================================================================== kuyruk ve sıra
def test_queue_plays_in_order_without_overlap(engines):
    output = FakeOutput()
    engine, _ = engines(output=output)
    jobs = [engine.say(f"Yanıt {n}.") for n in ("bir", "iki", "üç")]
    assert engine.wait_idle(5)
    sequence = [w[0] for w in output.writes]
    expected = [marker(job.spoken) for job in jobs]
    assert [k for i, k in enumerate(sequence) if i == 0 or sequence[i - 1] != k] == expected
    assert output.max_active == 1 and all(job.status == "done" for job in jobs)
    assert output.names().count("finish") == 3


def test_two_threads_speaking_at_once_do_not_mix(engines):
    output = FakeOutput()
    engine, _ = engines(FakeBackend(chunks=4, gen_delay=0.005), output)
    barrier = threading.Barrier(2)

    def speak(text):
        barrier.wait()
        engine.say(text)
    threads = [threading.Thread(target=speak, args=(t,)) for t in ("Süre uyarısı.", "Karar kaydedildi.")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert engine.wait_idle(5)
    sequence = [w[0] for w in output.writes]
    runs = [k for i, k in enumerate(sequence) if i == 0 or sequence[i - 1] != k]
    assert len(runs) == 2 and output.max_active == 1  # her yanıt kesintisiz, biri bitince diğeri


def test_say_does_not_block_command_loop(engines):
    engine, _ = engines(output=FakeOutput(speed=1.0))  # 0,3 sn gerçek zamanlı çalma
    started = time.monotonic()
    engine.say("Uzun bir yanıt.")
    assert time.monotonic() - started < 0.1
    assert engine.is_speaking and engine.wait_idle(5)


def test_blocking_mode_waits_for_playback(engines):
    engine, _ = engines(output=FakeOutput(speed=1.0), blocking=True)
    started = time.monotonic()
    job = engine.say("Demo yanıtı.")
    assert job.status == "done" and time.monotonic() - started >= 0.25


def test_full_queue_drops_oldest_pending_but_console_keeps_all(engines):
    engine, console = engines(output=FakeOutput(speed=1.0), max_queue=1)
    first = engine.say("Birinci.")
    assert wait_until(lambda: first.status == "playing")
    second = engine.say("İkinci.")
    third = engine.say("Üçüncü.")
    assert second.status == "dropped" and engine.stats.dropped == 1
    assert engine.wait_idle(5) and third.status == "done"
    assert all(t in console.getvalue() for t in ("Birinci.", "İkinci.", "Üçüncü."))


def test_cancel_all_stops_playback_and_generation(engines):
    backend = FakeBackend(chunks=20, gen_delay=0.05)  # üretim çalma sırasında sürüyor
    output = FakeOutput(speed=1.0)
    engine, _ = engines(backend, output)
    current = engine.say("Uzun okuma.")
    pending = engine.say("Bekleyen.")
    assert wait_until(lambda: current.status == "playing")
    started = time.monotonic()
    engine.cancel_all()
    assert engine.wait_idle(2) and time.monotonic() - started < 0.6
    assert current.status == "cancelled" and pending.status == "cancelled"
    assert "abort" in output.names() and backend.closed_early >= 1
    assert engine.say("Sonraki.") is not None and engine.wait_idle(5)


def test_shutdown_lets_last_answer_finish(engines):
    output = FakeOutput(speed=0.5)
    engine, _ = engines(output=output)
    job = engine.say("Görüşmek üzere.")
    assert engine.shutdown(5) is True and job.status == "done"
    assert wait_until(lambda: "close" in output.names())
    assert not engine._synth.is_alive() and not engine._player.is_alive()
    assert engine.say("Kapandıktan sonra.") is None


def test_shutdown_timeout_cuts_long_speech(engines):
    engine, _ = engines(FakeBackend(chunks=50), FakeOutput(speed=1.0))
    job = engine.say("Çok uzun yanıt.")
    started = time.monotonic()
    assert engine.shutdown(0.3) is False
    assert time.monotonic() - started < 3 and job.status == "cancelled"
    assert not engine._player.is_alive()


def test_shutdown_helper_and_ctrl_c():
    class Speech:
        stopped = False

        def shutdown(self, timeout):
            raise KeyboardInterrupt

        def stop(self):
            Speech.stopped = True
    shutdown_speech(Speech(), 1)
    assert Speech.stopped
    shutdown_speech(object(), 1)  # shutdown'ı olmayan nesne sorun çıkarmaz


# ====================================================================== yankı koruması
def test_not_muted_while_generating_only(engines):
    backend = FakeBackend(chunks=3, gen_delay=0.3)
    engine, _ = engines(backend, prebuffer_ms=150)  # ilk 2 parça gelmeden çalma başlamaz
    job = engine.say("Hesaplanıyor.")
    assert wait_until(lambda: job.status == "generating")
    time.sleep(0.1)
    assert not engine.is_playing and not engine.is_muted(0.5)
    assert wait_until(lambda: engine.is_playing, 3) and engine.is_muted(0.0)
    assert engine.wait_idle(5)


def test_muted_during_playback_and_guard_then_listens_again(engines):
    engine, _ = engines(output=FakeOutput(speed=1.0))
    job = engine.say("Mikrofon bu sırada dinlemez.")
    assert wait_until(lambda: engine.is_playing)
    assert engine.is_muted(0.0)
    assert engine.wait_idle(5) and job.status == "done"
    assert engine.is_muted(0.5)  # oynatma bitti, koruma süresi içinde
    time.sleep(0.15)
    assert not engine.is_muted(0.1)  # mikrofon yeniden dinliyor


def test_playback_end_is_after_device_drained(engines):
    output = FakeOutput(speed=0.5)
    engine, _ = engines(output=output)
    job = engine.say("Bitiş anı.")
    assert engine.wait_idle(5)
    finish_time = [t for name, t, _ in output.events if name == "finish"][-1]
    assert engine._last_play_end >= finish_time and job.playback_ended_at >= finish_time


def test_stuck_speaker_never_mutes_microphone_forever(engines):
    release = threading.Event()
    output = FakeOutput(block_write=release)
    engine, console = engines(output=output, stuck_after_s=0.3)
    engine.say("Takılan hoparlör.")
    assert wait_until(lambda: engine.is_playing)
    assert engine.is_muted(0.0)
    time.sleep(0.4)
    assert not engine.is_muted(0.0) and not engine.enabled and "takıldı" in engine.status_message
    release.set()
    assert engine.say("Sonraki yanıt.") is None and "Sonraki yanıt." in console.getvalue()


def test_generation_stall_mid_playback_unmutes(engines):
    backend = FakeBackend(chunks=3, hang_on="Takılan", hang_from=1)
    engine, _ = engines(backend, prebuffer_ms=50, stuck_after_s=0.3)
    job = engine.say("Takılan üretim.")
    assert wait_until(lambda: engine.is_playing)
    assert wait_until(lambda: not engine.is_playing, 2)
    assert not engine.is_muted(0.0) or engine.is_muted(0.7)  # yalnızca kısa koruma süresi kalır
    backend.hang_release.set()
    assert wait_until(lambda: job.done.is_set(), 5) and job.status == "cancelled" and "takıldı" in job.error
    assert engine.enabled and engine.say("Devam.") is not None and engine.wait_idle(5)


def _capture(frames, window, keep):
    source = FakeSource(frames)
    segments = []
    capture = AudioCapture(source, on_segment=segments.append, is_muted=lambda: source.position in window,
                           keep_partial_on_mute=keep)
    capture.start()
    capture.ended.wait(10)
    capture.stop()
    return segments


def test_user_speech_before_playback_is_kept_when_playback_is_precise():
    frames = silence(900) + tone(1200) + silence(1200) + tone(600) + silence(1200)
    window = range(50, 75)  # birinci konuşmanın ortasında hoparlör çalmaya başlıyor
    assert len(_capture(frames, window, keep=False)) == 1  # eski davranış (pyttsx3) değişmedi
    kept = _capture(frames, window, keep=True)
    assert len(kept) == 2  # yarım konuşma + sonraki temiz konuşma: mikrofon yeniden dinliyor
    assert kept[0].duration < 1.2 and not kept[0].final  # yalnızca oynatmadan ÖNCE söylenen kısım
    assert 0.5 <= kept[1].duration <= 1.5


def test_engines_declare_playback_precision():
    assert PocketSpeechEngine.precise_playback_events is True
    assert SpeechEngine.precise_playback_events is False


# ====================================================================== hatalar
def test_output_error_keeps_console_and_recovers(engines):
    output = FakeOutput(fail_start=1)
    engine, console = engines(output=output)
    failed = engine.say("İlk yanıt.")
    assert wait_until(lambda: failed.done.is_set()) and failed.status == "failed"
    ok = engine.say("İkinci yanıt.")
    assert engine.wait_idle(5) and ok.status == "done" and engine.enabled
    assert "İlk yanıt." in console.getvalue()


def test_repeated_output_errors_disable_speech(engines):
    engine, console = engines(output=FakeOutput(fail_start=5))
    for n in range(3):
        engine.say(f"Deneme {n}.")
        engine.wait_idle(2)
    assert not engine.enabled and "Hoparlör kullanılamıyor" in engine.status_message
    engine.say("Konsol yine çalışıyor.")
    assert "Konsol yine çalışıyor." in console.getvalue()


def test_write_error_mid_playback(engines):
    engine, _ = engines(output=FakeOutput(fail_write_on=1))
    job = engine.say("Kesilen yanıt.")
    assert wait_until(lambda: job.done.is_set()) and job.status == "failed" and "çalma hatası" in job.error
    assert not engine.is_playing


def test_model_error_on_one_answer_does_not_stop_others(engines):
    engine, console = engines(FakeBackend(fail_on="bozuk"))
    bad = engine.say("bozuk metin")
    good = engine.say("Sağlam metin.")
    assert engine.wait_idle(5)
    assert bad.status == "failed" and "model hatası" in bad.error and good.status == "done"
    assert "bozuk metin" in console.getvalue()


# ====================================================================== Türkçe metin ve normalizasyon
def test_turkish_characters_reach_model_intact(engines):
    backend = FakeBackend()
    engine, console = engines(backend)
    text = "Çağrı Şükrü Öztürk, İğdır ve Ilgaz'daki ığdırlı ölçüm ekibi."
    engine.say(text)
    assert engine.wait_idle(5)
    assert backend.requests[-1][0] == text and f"JARVIS: {text}" in console.getvalue()


@pytest.mark.parametrize("text,expected", [
    ("Son tarih 2026-10-22.", "Son tarih 22.10.2026."),
    ("Tutanak oluşturuldu: output/demo/2026-10-15_1402_x.pdf. Gönderin.",
     "Tutanak oluşturuldu: ekrandaki dosya. Gönderin."),
    ("Ayrıntı logs/jarvis.log dosyasında.", "Ayrıntı dosyasında."),
    ("Taslak: C:\\dev\\JARVIS\\output\\outbox\\a.eml", "Taslak: ekrandaki dosya."),
    ("Alıcı: ayse.yilmaz@example.com", "Alıcı: e-posta adresi."),
    ("J.A.R.V.I.S hazır. PDF ve SMTP hazır.", "Jarvis hazır. pe de ef ve es em te pe hazır."),
    ("lehte 5 → kabul", "lehte 5, kabul."),
    ("Sürenin %80'i doldu (12/15 dk). Madde 2/4.", "Sürenin %80'i doldu (12/15 dk). Madde 2/4."),
    ("Saat 14:30'da, 15.10.2026 tarihinde.", "Saat 14:30'da, 15.10.2026 tarihinde."),
])
def test_speech_normalization(text, expected):
    assert to_speech_text(text) == expected


def test_multiline_help_becomes_sentences_and_is_shortened():
    from jarvis.voice.command_parser import HELP_TEXT

    spoken = to_speech_text(HELP_TEXT, max_chars=200)
    assert "\n" not in spoken and spoken.endswith(MORE_ON_SCREEN) and len(spoken) <= 200 + len(MORE_ON_SCREEN) + 1


def test_normalization_does_not_touch_original():
    original = "Görev: Mehmet raporu 2026-10-22'ye kadar output/x.pdf olarak göndersin."
    copy = str(original)
    to_speech_text(original)
    assert original == copy
    assert speech_text.shorten("kısa", 10) == "kısa"


def test_database_and_transcript_keep_original_text(manager, minutes):
    backend = FakeBackend()
    engine, console = make(backend)
    try:
        assistant = JarvisAssistant(manager, CommandParser("jarvis", ["carvis"]), engine, minutes=minutes)
        assistant.handle_text("Jarvis toplantıyı başlat")
        text = "PDF raporu 2026-10-22 tarihinde output/rapor.pdf olarak paylaşılacak"
        response = assistant.handle_text(f"Jarvis karar: {text}")
        assistant.respond(response)
        assistant.handle_text("Bütçe tablosu ayse@example.com adresine 2026-10-20'de gidecek.")
        assert engine.wait_idle(5)
        decision = manager.meeting.decisions[-1].text
        assert "2026-10-22" in decision and "output/rapor.pdf" in decision and "PDF" in decision
        assert "ayse@example.com" in manager.meeting.transcript[-1].text
        assert "2026-10-22" in console.getvalue()  # konsolda özgün metin
        spoken = backend.requests[-1][0]
        assert "22.10.2026" in spoken and "output/" not in spoken
    finally:
        engine.stop()


# ====================================================================== pyttsx3 ortak arayüzü
def test_legacy_engine_has_wait_idle_and_shutdown():
    speech = SpeechEngine(enabled=False, output=io.StringIO())
    assert speech.wait_idle(0.1) is True and speech.shutdown(0.1) is True


# ====================================================================== performans aracı
def test_bench_separates_load_from_generation(tmp_path):
    backend = FakeBackend(load_delay=0.2, gen_delay=0.01)
    lines = []
    settings = Settings().with_overrides(output_dir=tmp_path)
    assert run_tts_benchmark(settings, runs=1, play=True, backend=backend, output=FakeOutput(speed=0.01),
                             print_func=lines.append, command_repeats=1, out_dir=tmp_path) == 0
    report = next(tmp_path.glob("tts_bench_*.md")).read_text(encoding="utf-8")
    assert "Model yükleme | 0.2" in report and "RTF" in report and "Komut işleme gecikmesi" in report
    assert next(tmp_path.glob("tts_bench_*.json")).exists()
    generation = measure_generation(backend, "Merhaba.", "male_1", "kısa", 1)
    assert generation.total_s < 0.2 and generation.audio_s == pytest.approx(0.3) and generation.rtf < 1


def test_bench_reports_missing_model(tmp_path):
    lines = []
    code = run_tts_benchmark(Settings(), backend=FakeBackend(load_error=missing_model()), print_func=lines.append,
                             out_dir=tmp_path)
    assert code == 1 and any("--tts-download" in line for line in lines)
