"""Ses yakalama ve bölütleme (mikrofon simülasyonu; gerçek aygıt kullanılmaz)."""
import threading
import time
import wave
from datetime import datetime, timedelta

from jarvis.voice.audio_capture import (
    AudioCapture,
    AudioSegment,
    Segmenter,
    SegmenterConfig,
    WavFileSource,
    frame_rms,
    measure_levels,
)
from tests.voice_fakes import FakeSource, silence, tone, utterances

T0 = datetime(2026, 10, 14, 10, 0, 0)


def feed_all(segmenter: Segmenter, frames: list[bytes]) -> tuple[list[AudioSegment], datetime]:
    now = T0
    out = []
    for frame in frames:
        now += timedelta(milliseconds=30)
        out += segmenter.feed(frame, now)
    return out, now


# ------------------------------------------------------------------ Segmenter
def test_frame_rms():
    assert frame_rms(b"") == 0
    assert frame_rms(silence(30)[0]) == 0
    assert 2500 < frame_rms(tone(30, amplitude=4000)[0]) < 3000


def test_calibration_sets_threshold_above_noise():
    segmenter = Segmenter()
    feed_all(segmenter, silence(900))
    assert segmenter.calibrated
    assert segmenter.threshold >= SegmenterConfig().min_threshold


def test_single_utterance_with_preroll_and_timing():
    segmenter = Segmenter()
    segments, _ = feed_all(segmenter, silence(900) + tone(1500) + silence(1200))
    assert len(segments) == 1
    seg = segments[0]
    assert 1.5 <= seg.duration <= 2.5  # konuşma + ön tampon + korunan 500 ms kuyruk
    speech_start = T0 + timedelta(milliseconds=900)
    assert abs((seg.started_at - speech_start).total_seconds()) < 0.4  # ilk hece kesilmez
    assert not seg.forced_cut and not seg.final


def test_two_utterances_split_by_silence():
    segmenter = Segmenter()
    segments, _ = feed_all(segmenter, utterances(2))
    assert len(segments) == 2


def test_short_click_is_ignored():
    segmenter = Segmenter()
    segments, _ = feed_all(segmenter, silence(900) + tone(90) + silence(1200))
    assert segments == []


def test_short_pause_inside_sentence_does_not_split():
    segmenter = Segmenter()
    segments, _ = feed_all(segmenter, silence(900) + tone(600) + silence(300) + tone(600) + silence(1200))
    assert len(segments) == 1


def test_long_speech_cut_at_max_length():
    segmenter = Segmenter(SegmenterConfig(max_segment_s=3))
    segments, _ = feed_all(segmenter, silence(900) + tone(7000) + silence(1200))
    assert len(segments) >= 2
    assert segments[0].forced_cut and segments[0].duration <= 3.1
    assert sum(s.duration for s in segments) >= 6.5  # konuşmanın tamamı korunur


def test_flush_returns_unfinished_speech():
    segmenter = Segmenter()
    _, now = feed_all(segmenter, silence(900) + tone(800))
    final = segmenter.flush(now)
    assert final is not None and final.final
    assert segmenter.flush(now) is None


def test_discard_drops_partial_segment():
    segmenter = Segmenter()
    _, now = feed_all(segmenter, silence(900) + tone(600))
    segmenter.discard()
    segments, _ = feed_all(segmenter, silence(1200))
    assert segments == [] and segmenter.flush(now) is None


def test_fixed_threshold_skips_calibration():
    segmenter = Segmenter(SegmenterConfig(energy_threshold=500))
    segments, _ = feed_all(segmenter, tone(600) + silence(1200))
    assert len(segments) == 1 and segmenter.threshold == 500


def test_wav_roundtrip(tmp_path):
    segmenter = Segmenter()
    segment = feed_all(segmenter, utterances(1))[0][0]
    path = segment.save_wav(tmp_path / "a.wav")
    with wave.open(str(path)) as wav:
        assert wav.getframerate() == 16000 and wav.getnchannels() == 1
        assert wav.getnframes() * 2 == len(segment.pcm)


# ------------------------------------------------------------------ AudioCapture (iş parçacığı)
def run_capture(source, **kwargs):
    segments, statuses = [], []
    capture = AudioCapture(source, on_segment=segments.append, on_status=statuses.append, max_backoff_s=0.05,
                           **kwargs)
    capture.start()
    capture.ended.wait(10)
    capture.stop()
    return segments, statuses, capture


def test_capture_delivers_segments_in_order():
    segments, statuses, capture = run_capture(FakeSource(utterances(3)))
    assert len(segments) == 3
    assert segments[0].started_at < segments[1].started_at < segments[2].started_at
    assert statuses == []


def test_microphone_missing_at_start_is_retried():
    segments, statuses, capture = run_capture(FakeSource(utterances(1), fail_opens=2))
    assert len(segments) == 1
    assert any("kullanılamıyor" in s for s in statuses)
    assert any("yeniden bağlandı" in s for s in statuses)


def test_microphone_disconnect_mid_meeting_recovers():
    frames = utterances(2)
    source = FakeSource(frames, fail_at={len(frames) // 2})
    segments, statuses, capture = run_capture(source)
    assert capture.disconnects == 1
    assert any("koptu" in s for s in statuses)
    assert any("Dinlemeye devam" in s for s in statuses)
    assert len(segments) >= 2  # kopmadan önceki konuşma kaybolmaz


def test_muted_frames_are_ignored():
    muted = threading.Event()
    muted.set()
    segments, _, _ = run_capture(FakeSource(utterances(2)), is_muted=muted.is_set)
    assert segments == []


def test_mute_during_speech_discards_partial_segment():
    frames = silence(900) + tone(1200) + silence(1200) + tone(600) + silence(1200)
    source = FakeSource(frames)
    mute_window = range(40, 60)  # ilk konuşmanın ortası
    segments, _, _ = run_capture(source, is_muted=lambda: source.position in mute_window)
    assert len(segments) == 1  # yalnızca ikinci (temiz) konuşma


def test_stop_flushes_unfinished_speech():
    gate = threading.Event()
    source = FakeSource(silence(900) + tone(900), block_at_end=gate)
    segments = []
    capture = AudioCapture(source, on_segment=segments.append)
    capture.start()
    time.sleep(0.2)
    gate.set()
    capture.stop()
    assert len(segments) == 1 and segments[0].final


def test_wav_file_source_and_levels(tmp_path):
    segment = AudioSegment(pcm=b"".join(silence(600) + tone(600)), sample_rate=16000,
                           started_at=T0, ended_at=T0)
    path = segment.save_wav(tmp_path / "s.wav")
    report = measure_levels(WavFileSource(path, tail_silence_ms=0), seconds=5)
    assert report.noise_floor < 10 and report.peak > 2000
    assert report.suggested_threshold >= 120
