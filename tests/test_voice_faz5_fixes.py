"""Faz 4 saha bulgularına yönelik düzeltmelerin testleri (kelime sonu kesilmesi, yeniden örnekleme,
--mic-check başarı ölçütü, test raporunda hata türleri, kesik komut tespiti, Bluetooth uyarısı)."""
from __future__ import annotations

from array import array
from datetime import datetime

import pytest

from config.settings import load_settings
from jarvis.voice.audio_capture import (
    AudioSegment,
    Segmenter,
    SegmenterConfig,
    bluetooth_warning,
    resample_pcm16,
)
from jarvis.voice.command_parser import CommandParser
from jarvis.voice.stt import GoogleSpeechToText
from jarvis.voice.truncation import detect_truncation
from jarvis.voice.voice_test import (
    ItemResult,
    VoiceTestItem,
    evaluate,
    evaluate_result,
    run_mic_check,
    summarize,
    write_report,
)
from tests.test_voice_test_tool import PromptedSource
from tests.voice_fakes import FakeStt, network_error, silence, tone

PARSER = CommandParser("jarvis", ["carvis"])
T0 = datetime(2026, 10, 14, 10, 0, 0)


def feed(segmenter: Segmenter, frames: list[bytes]) -> list[AudioSegment]:
    out = []
    now = T0
    from datetime import timedelta

    for frame in frames:
        now += timedelta(milliseconds=30)
        out += segmenter.feed(frame, now)
    return out


# ---------------------------------------------------------------- kelime sonu (VAD kuyruğu ve histerezis)
def test_quiet_word_ending_is_kept_in_segment():
    """Yüksek sesli kısımdan sonra eşiğin altında kalan zayıf hece (ör. "başla-t") bölütte kalmalı."""
    loud, quiet = tone(900, amplitude=4000), tone(240, amplitude=900)
    frames = silence(900) + loud + quiet + silence(1200)
    new = feed(Segmenter(SegmenterConfig(energy_threshold=1500)), frames)
    old = feed(Segmenter(SegmenterConfig(energy_threshold=1500, tail_keep_ms=200, release_ratio=0.75)), frames)
    assert len(new) == 1 and len(old) == 1
    # Yeni ayarlar: zayıf hece konuşma sayılır + 500 ms kuyruk korunur
    assert new[0].duration >= 0.9 + 0.24 + 0.45
    # Eski ayarlar zayıf heceyi sessizlik sayıp yalnızca 200 ms bırakıyordu (kesilme)
    assert old[0].duration < 0.9 + 0.3 + 0.24
    # Kullanıcının sustuğu an, korunan sessizlikten önce olmalı (gecikme ölçümü için)
    assert new[0].voice_ended_at < new[0].ended_at


def test_release_level_never_below_noise_floor():
    segmenter = Segmenter(SegmenterConfig(release_ratio=0.2))
    feed(segmenter, tone(600, amplitude=300))  # kalibrasyon: gürültü tabanı ~212
    assert segmenter.noise_floor is not None
    assert segmenter.release_level >= segmenter.noise_floor * 1.3


def test_settings_expose_vad_and_resample(monkeypatch):
    monkeypatch.setenv("JARVIS_ENV_FILE", "none")
    monkeypatch.setenv("VAD_TAIL_KEEP_MS", "650")
    monkeypatch.setenv("VAD_RELEASE_PERCENT", "40")
    monkeypatch.setenv("STT_RESAMPLE_HZ", "0")
    monkeypatch.setenv("STT_SLOW_WARNING_S", "3,5")
    s = load_settings()
    assert (s.vad_tail_keep_ms, s.vad_release_percent, s.stt_resample_hz, s.stt_slow_warning_s) == (650, 40, 0, 3.5)
    from jarvis.voice.voice_listener import segmenter_config_from

    config = segmenter_config_from(s)
    assert config.tail_keep_ms == 650 and config.release_ratio == 0.4


# ---------------------------------------------------------------- yeniden örnekleme
def _samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm)
    return a


def test_resample_48k_to_16k_averages_and_keeps_duration():
    pcm = array("h", [300, 600, 900] * 1600).tobytes()  # 0.1 sn @ 48 kHz
    out = _samples(resample_pcm16(pcm, 48000, 16000))
    assert len(out) == 1600 and set(out) == {600}


def test_resample_44100_to_16000_length_and_range():
    pcm = array("h", [32767, -32768] * 22050).tobytes()  # 1 sn @ 44.1 kHz
    out = _samples(resample_pcm16(pcm, 44100, 16000))
    assert abs(len(out) - 16000) <= 1
    assert max(out) <= 32767 and min(out) >= -32768
    # Alçak geçiren süzgeç: en yüksek frekanslı işaret büyük ölçüde sönümlenir (örtüşme azalır)
    assert max(abs(v) for v in out) < 20000


def test_resample_same_rate_is_noop():
    pcm = b"\x01\x00\x02\x00"
    assert resample_pcm16(pcm, 16000, 16000) is pcm


class _FakeSr:
    class UnknownValueError(Exception):
        pass

    class RequestError(Exception):
        pass

    class AudioData:
        def __init__(self, pcm, rate, width):
            self.pcm, self.rate, self.width = pcm, rate, width

    class Recognizer:
        def __init__(self):
            self.seen = None

        def recognize_google(self, audio, key=None, language=None, show_all=False):
            self.seen = audio
            return {"alternative": [{"transcript": "Jarvis gündemi oku", "confidence": 0.9}]}


def test_google_stt_sends_16k_audio():
    recognizer = _FakeSr.Recognizer()
    stt = GoogleSpeechToText(recognizer=recognizer, sr_module=_FakeSr, resample_hz=16000)
    segment = AudioSegment(pcm=b"\x00\x00" * 48000, sample_rate=48000, started_at=T0, ended_at=T0)
    result = stt.transcribe(segment)
    assert recognizer.seen.rate == 16000 and len(recognizer.seen.pcm) == 2 * 16000
    assert result.sent_rate == 16000 and abs(result.audio_s - 1.0) < 0.01


def test_google_stt_resample_can_be_disabled():
    recognizer = _FakeSr.Recognizer()
    stt = GoogleSpeechToText(recognizer=recognizer, sr_module=_FakeSr, resample_hz=0)
    stt.transcribe(AudioSegment(pcm=b"\x00\x00" * 4800, sample_rate=48000, started_at=T0, ended_at=T0))
    assert recognizer.seen.rate == 48000


# ---------------------------------------------------------------- kesik komut
@pytest.mark.parametrize("heard,example", [
    ("toplantıyı başl", "toplantıyı başlat"), ("önceki toplantıyı özet", "önceki toplantıyı özetle"),
    ("gündemi o", "gündemi oku"), ("açık görevleri", "açık görevleri oku"),
])
def test_truncated_command_detected_with_example(heard, example):
    found = detect_truncation(heard)
    assert found is not None and found.example == example
    assert "Hiçbir işlem yapılmadı" in found.message()


@pytest.mark.parametrize("heard", ["sözü", "söz", "görev", "karar", "Son tarih"])
def test_command_word_without_parameter_is_truncated(heard):
    found = detect_truncation(heard)
    assert found is not None and "duyulmadı" in found.missing


def test_ambiguous_prefix_gives_no_example():
    found = detect_truncation("toplantıyı b")  # başlat mı bitir mi? tahmin edilmez
    assert found is not None and found.example == ""


@pytest.mark.parametrize("heard", ["", "top", "bütçe nasıl gidiyor", "toplantıyı başlat"])
def test_not_truncated(heard):
    assert detect_truncation(heard) is None


def test_assistant_does_not_execute_truncated_command(assistant):
    response = assistant.handle_text("Jarvis toplantıyı başl")
    assert not response.ok and "eksik" in response.message
    assert assistant.manager.meeting is None  # toplantı başlatılmadı (otomatik tamamlama yok)
    response = assistant.handle_text("Jarvis toplantıyı b")
    assert "Örneğin" not in response.message


# ---------------------------------------------------------------- doğruluk testi raporu
def test_network_error_is_not_reported_as_unintelligible(tmp_path):
    item = VoiceTestItem("Jarvis gündemi oku", ["READ_AGENDA"])
    evaluation = evaluate_result(item, None, PARSER, "ağ hatası: internet yok")
    assert "ağ hatası" in evaluation.reason and not evaluation.correct
    results = [
        ItemResult(1, item, None, [], evaluation, None, None, error="ağ hatası: internet yok"),
        ItemResult(2, item, None, [], evaluate(item, None, PARSER), None, None),
        ItemResult(3, VoiceTestItem("Jarvis toplantıyı başlat", ["START_MEETING"]), "Jarvis toplantıyı başl", [],
                   evaluate(VoiceTestItem("x", ["START_MEETING"]), "Jarvis toplantıyı başl", PARSER), 5.2, 6.0,
                   extra_segments=1),
    ]
    summary = summarize(results, 0, slow_warning_s=4.0)
    assert (summary.network_errors, summary.unintelligible, summary.truncated) == (1, 1, 1)
    assert summary.slow_stt == 1 and summary.split_segments == 1 and summary.correct_commands == 0
    md, csv_path, _ = write_report(results, 0, tmp_path, {"device": "x", "engine": "test"}, PARSER)
    text = md.read_text(encoding="utf-8")
    assert "STT/ağ hatası" in text and "Kesilmiş komut" in text and "İkiye bölünen" in text
    assert "Ek bölüt" in csv_path.read_text(encoding="utf-8-sig")


def test_bluetooth_warning_detection():
    assert bluetooth_warning("Kulaklık (AirPods Pro) Hands-Free AG Audio")
    assert bluetooth_warning("Headset Microphone (WH-1000XM4)")
    assert bluetooth_warning("Mikrofon (USB Masa Mikrofonu)") is None
    assert bluetooth_warning("") is None


# ---------------------------------------------------------------- --mic-check
class NoisySource(PromptedSource):
    """Boşta hafif oda gürültüsü veren sahte mikrofon (tamamen sessiz mikrofon zaten hata sayılır)."""

    def read(self):
        frame = super().read()
        if frame == silence(30)[0]:
            return tone(30, amplitude=40)[0]
        return frame


class _Speech:
    def __init__(self, enabled=True, voice_name="Microsoft Tolga"):
        self.enabled = enabled
        self.voice_name = voice_name
        self.status_message = f"Türkçe ses: {voice_name}" if voice_name else "Türkçe ses bulunamadı"
        self.said = []

    def say(self, text, wait=True):
        self.said.append(text)


def _printer(source, printed):
    """Kullanıcı "Dinleniyor..." yazısını görünce konuşur (gerçek kullanım sırası)."""
    def emit(message):
        printed.append(message)
        if "Dinleniyor" in message:
            source.speak()
    return emit


def _mic_check(stt_text, speech=None, source=None):
    source = source or NoisySource()
    printed = []
    code = run_mic_check(source, FakeStt([stt_text]), PARSER, speech or _Speech(),
                         print_func=_printer(source, printed), input_func=lambda prompt: "")
    return code, "\n".join(printed)


def test_mic_check_passes_only_on_expected_intent():
    code, out = _mic_check("Jarvis gündemi oku")
    assert code == 0 and "READ_AGENDA ✓" in out and "her şey çalışıyor" in out


def test_silent_microphone_is_failure():
    code, out = _mic_check("Jarvis gündemi oku", source=PromptedSource())
    assert code == 1 and "hiç ses gelmiyor" in out


def test_mic_check_unknown_is_failure():
    """Faz 4 hatası: UNKNOWN sonucu başarı sayılıyordu."""
    code, out = _mic_check("Jarvis gündemi okumak")
    assert code == 1 and "UNKNOWN" in out and "1 sorun" in out


def test_mic_check_truncated_and_wrong_intent_are_failures():
    code, out = _mic_check("Jarvis gündemi o")
    assert code == 1 and "kesilmiş" in out
    code, out = _mic_check("Jarvis süre durumu")
    assert code == 1 and "TIME_STATUS" in out


def test_mic_check_network_error_is_failure():
    source = NoisySource()
    printed = []
    stt = FakeStt([network_error(), network_error(), network_error()])
    import jarvis.voice.voice_test as vt

    original = vt.time.sleep
    vt.time.sleep = lambda s: None  # yeniden deneme beklemesini kısalt
    try:
        code = run_mic_check(source, stt, PARSER, _Speech(), print_func=_printer(source, printed),
                             input_func=lambda p: "")
    finally:
        vt.time.sleep = original
    out = "\n".join(printed)
    assert code == 1 and "ağ hatası" in out and "1 sorun" in out


def test_mic_check_missing_turkish_voice_is_failure():
    code, out = _mic_check("Jarvis gündemi oku", speech=_Speech(voice_name=None))
    assert code == 1 and "Türkçe TTS sesi bulunamadı" in out


def test_mic_check_warns_for_bluetooth():
    source = NoisySource()
    source.name = "Kulaklık (AirPods) Hands-Free"
    code, out = _mic_check("Jarvis gündemi oku", source=source)
    assert code == 0 and "Bluetooth" in out and "1 uyarı" in out
