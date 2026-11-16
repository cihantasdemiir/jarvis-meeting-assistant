"""Girdi kaynakları.

Tüm kaynaklar aynı arayüzü uygular: `listen()` bir metin satırı döndürür,
girdi bittiğinde `None` döndürür. Böylece üst katman (JarvisAssistant)
girdinin klavyeden mi, mikrofondan mı, yoksa test senaryosundan mı geldiğini
bilmez.

Bu dosyada (Faz 1):
  * TextListener     — klavye modu (`--text`)
  * ScriptedListener — demo modu ve testler için hazır komut listesi
  * list_microphones — `--list-mics`

Mikrofon tabanlı dinleyiciler (Google STT ve faster-whisper) Faz 4'te
`voice_listener.py` / `whisper_listener.py` içinde eklenecektir.
"""
from __future__ import annotations

from typing import Callable, Iterable, Protocol


class InputSource(Protocol):
    def listen(self) -> str | None: ...

    def close(self) -> None: ...


class TextListener:
    """Klavyeden satır okur. Ctrl+Z/Ctrl+D (EOF) girdinin bittiğini bildirir."""

    def __init__(self, prompt: str = "Siz > ", input_func: Callable[[str], str] = input):
        self.prompt = prompt
        self._input = input_func

    def listen(self) -> str | None:
        try:
            return self._input(self.prompt)
        except EOFError:
            return None

    def close(self) -> None:
        pass


class ScriptedListener:
    """Önceden hazırlanmış satırları sırayla döndürür (demo ve testler)."""

    def __init__(self, lines: Iterable[str], echo: Callable[[str], None] | None = None):
        self._lines = list(lines)
        self._index = 0
        self._echo = echo

    def listen(self) -> str | None:
        if self._index >= len(self._lines):
            return None
        line = self._lines[self._index]
        self._index += 1
        if self._echo:
            self._echo(line)
        return line

    def close(self) -> None:
        pass


def list_microphones() -> tuple[list[str], str | None]:
    """Kullanılabilir mikrofonları döndürür: (isimler, hata mesajı)."""
    try:
        import speech_recognition as sr  # noqa: PLC0415
    except ImportError:
        return [], ("SpeechRecognition kurulu değil. Kurulum: pip install SpeechRecognition PyAudio")
    try:
        names = sr.Microphone.list_microphone_names()
    except Exception as exc:  # PyAudio eksik veya ses aygıtı yok
        return [], f"Mikrofonlar listelenemedi: {exc}. PyAudio kurulumunu kontrol edin."
    return list(names), None
