"""Türkçe karakter destekli TTF fontu bulma ve ReportLab'a kaydetme.

ReportLab'ın yerleşik Helvetica fontu "ğ, ş, ı, İ" harflerini içermez. Bu
yüzden PDF'te her zaman Unicode TTF font kullanılır. Arama sırası:

  1. .env: PDF_FONT_REGULAR / PDF_FONT_BOLD
  2. Proje: assets/fonts/DejaVuSans.ttf, DejaVuSans-Bold.ttf
  3. Windows: Arial, Calibri, Segoe UI, Tahoma (C:\\Windows\\Fonts)
  4. Linux/macOS: DejaVu, Liberation, Noto

Bulunan her font, Türkçe harflerin tamamını içerip içermediği kontrol
edilerek kabul edilir. İnternetten font indirilmez.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REGULAR_NAME = "JarvisTR"
BOLD_NAME = "JarvisTR-Bold"
TURKISH_CHARS = "çÇğĞıİöÖşŞüÜâî"

_WINDOWS_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_PROJECT_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"

# (normal, kalın) çiftleri — sıra önceliği belirler
_CANDIDATES: list[tuple[Path, Path]] = [
    (_PROJECT_FONT_DIR / "DejaVuSans.ttf", _PROJECT_FONT_DIR / "DejaVuSans-Bold.ttf"),
    (_WINDOWS_FONT_DIR / "arial.ttf", _WINDOWS_FONT_DIR / "arialbd.ttf"),
    (_WINDOWS_FONT_DIR / "calibri.ttf", _WINDOWS_FONT_DIR / "calibrib.ttf"),
    (_WINDOWS_FONT_DIR / "segoeui.ttf", _WINDOWS_FONT_DIR / "segoeuib.ttf"),
    (_WINDOWS_FONT_DIR / "tahoma.ttf", _WINDOWS_FONT_DIR / "tahomabd.ttf"),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
     Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    (Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf")),
    (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
     Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")),
    (Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
     Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")),
    (Path("/Library/Fonts/Arial Unicode.ttf"), Path("/Library/Fonts/Arial Bold.ttf")),
]


class FontError(RuntimeError):
    """Türkçe destekli font bulunamadığında fırlatılır."""


@dataclass(frozen=True)
class FontSet:
    regular: str
    bold: str
    regular_path: Path
    bold_path: Path


def _supports_turkish(path: Path) -> bool:
    from reportlab.pdfbase.ttfonts import TTFont  # noqa: PLC0415

    try:
        font = TTFont(f"_probe_{abs(hash(str(path)))}", str(path))
    except Exception as exc:  # bozuk veya desteklenmeyen font dosyası
        log.warning("Font okunamadı: %s (%s)", path, exc)
        return False
    char_map = getattr(font.face, "charToGlyph", {}) or {}
    return all(ord(ch) in char_map for ch in TURKISH_CHARS)


def _candidates(regular: Path | None, bold: Path | None) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    if regular is not None:
        pairs.append((regular, bold or regular))
    pairs.extend(_CANDIDATES)
    return pairs


_registered: FontSet | None = None


def register_turkish_fonts(regular: Path | None = None, bold: Path | None = None) -> FontSet:
    """Uygun fontu bulur, ReportLab'a kaydeder ve adlarını döndürür."""
    global _registered
    from reportlab.lib.fonts import addMapping  # noqa: PLC0415
    from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415
    from reportlab.pdfbase.ttfonts import TTFont  # noqa: PLC0415

    if _registered is not None and regular is None:
        return _registered

    tried: list[str] = []
    for regular_path, bold_path in _candidates(regular, bold):
        if not regular_path.is_file():
            continue
        tried.append(str(regular_path))
        if not _supports_turkish(regular_path):
            log.warning("Font Türkçe karakterleri içermiyor, atlandı: %s", regular_path)
            continue
        if not bold_path.is_file() or not _supports_turkish(bold_path):
            bold_path = regular_path  # kalın yoksa normal font kullanılır
        pdfmetrics.registerFont(TTFont(REGULAR_NAME, str(regular_path)))
        pdfmetrics.registerFont(TTFont(BOLD_NAME, str(bold_path)))
        addMapping(REGULAR_NAME, 0, 0, REGULAR_NAME)
        addMapping(REGULAR_NAME, 1, 0, BOLD_NAME)
        addMapping(REGULAR_NAME, 0, 1, REGULAR_NAME)
        addMapping(REGULAR_NAME, 1, 1, BOLD_NAME)
        fonts = FontSet(REGULAR_NAME, BOLD_NAME, regular_path, bold_path)
        if regular is None:
            _registered = fonts
        log.info("PDF fontu: %s / %s", regular_path, bold_path)
        return fonts

    raise FontError(
        "Türkçe karakterleri destekleyen bir TTF font bulunamadı. "
        "DejaVuSans.ttf ve DejaVuSans-Bold.ttf dosyalarını assets/fonts/ klasörüne koyun "
        "veya .env içinde PDF_FONT_REGULAR / PDF_FONT_BOLD yollarını belirtin. "
        f"Denenen dosyalar: {', '.join(tried) or 'yok'}"
    )
