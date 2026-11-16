"""Kesilmiş komut tespiti (Faz 4 saha bulgusu: "toplantıyı başl", "önceki toplantıyı özet", "Jarvis sözü").

Bu modül yalnızca TEŞHİS koyar; komutu tamamlamaz ve çalıştırmaz. Kesik bir komut
tahminle tamamlanırsa yanlış işlem yapılabilir (ör. "toplantıyı b..." → "bitir" mi "başlat" mı?).
Kullanıcıdan komutu tekrar söylemesi istenir. Kesik komutlar LLM'ye de gönderilmez:
LLM'nin eksik kelimeyi "tahmin etmesi" de bir otomatik tamamlamadır.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..utils.turkish_text import normalize_spaces, tr_lower

# Parametresiz tam komutlar (önek eşleşmesi için)
_FULL_COMMANDS = (
    "toplantıyı başlat", "toplantıyı bitir", "toplantıyı kapat", "toplantıyı sonlandır",
    "gündemi oku", "sıradaki madde", "sonraki madde", "önceki madde", "maddeyi ertele",
    "önceki toplantıyı özetle", "geçen toplantıyı özetle", "açık görevleri oku", "devreden görevler",
    "yoklama al", "kim konuşmadı", "konuşma süreleri", "kim konuşuyor", "sözü bırak", "süre durumu",
    "tutanağı gönder", "tutanağı tekrar gönder", "tutanak oluştur", "geri al", "yardım",
)

# Parametre gerektiren komut kökleri: yalnız başına söylenirse komut eksiktir.
_NEEDS_PARAMETER = {
    "söz": "kime söz verileceği", "sözü": "kime söz verileceği", "karar": "karar metni",
    "görev": "görev metni", "aksiyon": "görev metni", "not": "not metni", "otopark": "konu",
    "oylama": "oylama konusu ve oylar", "sorumlu": "sorumlu kişi", "son tarih": "tarih",
    "gündeme ekle": "madde başlığı", "katılımcılar": "katılımcı adları", "sonraki toplantı": "tarih",
    "görev tamamlandı": "hangi görev olduğu", "görev iptal": "hangi görev olduğu",
}

_MIN_PREFIX_CHARS = 4


@dataclass(frozen=True)
class Truncation:
    heard: str
    missing: str  # eksik görünen kısım (açıklama)
    example: str = ""  # kullanıcıya gösterilecek örnek tam komut (ÇALIŞTIRILMAZ)

    def message(self) -> str:
        hint = f" Örneğin: «Jarvis, {self.example}»." if self.example else ""
        return (f"Komut eksik algılandı («{self.heard}»: {self.missing}). Hiçbir işlem yapılmadı; "
                f"lütfen komutu baştan söyleyin.{hint}")


def detect_truncation(command: str) -> Truncation | None:
    """Uyandırma kelimesinden sonraki, ayrıştırıcının anlayamadığı metni inceler."""
    heard = normalize_spaces(command or "").strip(" .,:;!?-")
    text = tr_lower(heard)
    if not text:
        return None
    if text in _NEEDS_PARAMETER:
        return Truncation(heard, f"{_NEEDS_PARAMETER[text]} duyulmadı")
    if len(text) < _MIN_PREFIX_CHARS:
        return None
    matches = [c for c in _FULL_COMMANDS if c.startswith(text) and c != text]
    if matches:
        return Truncation(heard, "son kısmı kesilmiş görünüyor", matches[0] if len(matches) == 1 else "")
    return None
