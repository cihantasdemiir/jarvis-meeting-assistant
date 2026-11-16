"""İSTEĞE BAĞLI duman testi: gerçek Pocket TTS Turkish modeli ile (Faz 5b).

Varsayılan olarak ATLANIR. Çalıştırmak için (paket ve model kurulu Windows ortamında):

    $env:JARVIS_TTS_SMOKE="1"; python -m pytest tests/test_tts_pocket_smoke.py -v; Remove-Item Env:JARVIS_TTS_SMOKE

Hoparlöre ses ÇALMAZ; yalnızca modeli yerelden yükler, `male_1` ile ses üretir ve WAV dosyasına yazar.
Sesin doğallığı ve akıcılığı bu testle değil, dinleyerek değerlendirilir:
    python main.py --tts-bench --tts-play
"""
from __future__ import annotations

import os
import wave

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("JARVIS_TTS_SMOKE") != "1",
                                reason="gerçek TTS duman testi: JARVIS_TTS_SMOKE=1 ile çalıştırın")


def test_real_model_generates_turkish_audio(tmp_path):
    from config.settings import load_settings
    from jarvis.voice.pocket_backend import to_pcm16
    from jarvis.voice.speech_text import to_speech_text
    from jarvis.voice.tts_factory import default_backend

    settings = load_settings()
    backend = default_backend(settings)
    backend.load()  # internete çıkmaz; model yoksa TtsModelMissing ile anlaşılır hata verir
    assert settings.tts_voice in backend.voices and backend.sample_rate == 24000
    text = to_speech_text("Merhaba Kenan Bey. Ben Jarvis. Toplantımız başladı; gündemde 3 madde var.")
    pcm = b"".join(to_pcm16(chunk) for chunk in backend.stream(text, settings.tts_voice))
    seconds = len(pcm) / 2 / backend.sample_rate
    assert 1.5 < seconds < 20
    assert max(abs(int.from_bytes(pcm[i:i + 2], "little", signed=True)) for i in range(0, len(pcm), 200)) > 500
    path = tmp_path / "jarvis_smoke.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(backend.sample_rate)
        wav.writeframes(pcm)
    print(f"\nDuman testi sesi ({seconds:.1f} sn): {path}")
