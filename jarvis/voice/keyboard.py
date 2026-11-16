"""Klavyeden sesli yanıtı durdurma (Esc) — Windows konsolu.

İki kullanım:
  * Sesli kip: `KeyWatcher` arka planda tuşları yoklar. Esc → seslendirme kesilir. Bu kipte klavyeden komut
    okunmadığı için tuşları tüketmek başka bir şeyi bozmaz.
  * Klavye kipi (`--text`): `input()` ile ayrı bir tuş okuyucu aynı konsol tamponundan okumaya çalışırsa
    karakterler kaybolur. Bu yüzden Windows'ta satır `ConsoleLineReader` ile okunur: tek okuyucu satırı
    kendisi kurar, Esc'i ayrıca yakalar.

Ctrl+C: `msvcrt.getwch()` Ctrl+C'yi okuyamaz; Windows onu sinyal olarak ana iş parçacığına iletir. Okuyucular
kısa aralıklarla yokladığı (`kbhit` + kısa uyku) için KeyboardInterrupt hemen yükselir. Uygulamayı kapatma
davranışı değişmez. Esc yalnızca sesi keser; uygulamayı kapatmaz.

Windows dışı sistemlerde (Linux/macOS test ortamı) Esc desteklenmez; klavye kipi normal `input()` kullanır.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, TextIO

log = logging.getLogger(__name__)

ESC = "\x1b"
_SPECIAL_PREFIX = ("\x00", "\xe0")  # ok/işlev tuşları: ardından gelen kod okunup atılır
_POLL_S = 0.05


def _msvcrt():
    try:
        import msvcrt  # noqa: PLC0415

        return msvcrt
    except ImportError:
        return None


def esc_supported() -> bool:
    return _msvcrt() is not None and sys.stdin is not None and sys.stdin.isatty()


class KeyWatcher(threading.Thread):
    """Sesli kipte Esc tuşunu izler. Diğer tuşlar yok sayılır."""

    def __init__(self, on_escape: Callable[[], None], kbhit: Callable[[], bool] | None = None,
                 getwch: Callable[[], str] | None = None, poll_s: float = _POLL_S):
        super().__init__(daemon=True, name="key-watcher")
        module = _msvcrt()
        self._kbhit = kbhit or (module.kbhit if module else (lambda: False))
        self._getwch = getwch or (module.getwch if module else (lambda: ""))
        self.on_escape = on_escape
        self.poll_s = poll_s
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self._kbhit():
                    self._stop_event.wait(self.poll_s)
                    continue
                key = self._getwch()
                if key in _SPECIAL_PREFIX:
                    self._getwch()
                elif key == ESC:
                    self.on_escape()
            except Exception:  # tuş izleyicisi asla programı düşürmez
                log.exception("Tuş izleyici hatası")
                self._stop_event.wait(0.5)

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


class ConsoleLineReader:
    """`input()` yerine: satırı tuş tuş okur. Esc → `on_escape()` (yazılan satır korunur), Enter → satır,
    Backspace → sil, Ctrl+Z → girdi sonu (None). Türkçe karakterler `getwch` ile doğrudan okunur."""

    def __init__(self, on_escape: Callable[[], None], kbhit: Callable[[], bool] | None = None,
                 getwch: Callable[[], str] | None = None, output: TextIO | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = _POLL_S):
        module = _msvcrt()
        self._kbhit = kbhit or (module.kbhit if module else (lambda: True))
        self._getwch = getwch or (module.getwch if module else (lambda: sys.stdin.read(1) or "\x1a"))
        self.on_escape = on_escape
        self.output = output or sys.stdout
        self._sleep = sleep
        self.poll_s = poll_s

    def __call__(self, prompt: str = "") -> str:
        self._write(prompt)
        buffer: list[str] = []
        while True:
            while not self._kbhit():
                self._sleep(self.poll_s)  # Ctrl+C burada KeyboardInterrupt olarak yükselir
            key = self._getwch()
            if key in ("\r", "\n"):
                self._write("\n")
                return "".join(buffer)
            if key == "\x1a" and not buffer:  # Ctrl+Z: girdi sonu (eski input() davranışı)
                self._write("\n")
                raise EOFError
            if key == "\x03":  # bazı konsollarda Ctrl+C karakter olarak gelir
                raise KeyboardInterrupt
            if key == ESC:
                self.on_escape()
                continue
            if key in _SPECIAL_PREFIX:
                self._getwch()
                continue
            if key == "\x08":
                if buffer:
                    buffer.pop()
                    self._write("\b \b")
                continue
            if key.isprintable():
                buffer.append(key)
                self._write(key)

    def _write(self, text: str) -> None:
        self.output.write(text)
        self.output.flush()
