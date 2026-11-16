"""Hitapsız ("Jarvis" denmeden) söylenen cümlenin komut kalıbında olup olmadığını belirler (Faz 5c.2 canlı düzeltme).

Canlı testte "Ömer katıldı", "toplantı özeti çıkar", "kişi ekle Ahmet Mehmet Dursun" gibi cümleler transkripte
sıradan konuşma olarak yazıldı. Nedeni: STT "Jarvis" sözcüğünü düşürdü ya da kullanıcı hitap etmedi; sistem bunu
bilemez. Bu yüzden:
  * cümle hiçbir zaman kendiliğinden komut olarak ÇALIŞTIRILMAZ;
  * transkriptten silinmez, "komut gibi duyuldu" diye İŞARETLENİR (analiz ve istatistikte konuşma sayılmaz);
  * kurallı ayrıştırıcının tanıdığı, önemli olmayan bir komutsa kullanıcıya onay önerisi sunulabilir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.router import EXPLICIT_ONLY_INTENTS
from ..utils.turkish_text import tr_lower
from .command_parser import CommandParser, Intent, IntentType

# Hitapsız söylendiğinde onay önerilebilecek, kayıt içeriği taşımayan komutlar
PROPOSABLE = frozenset({
    IntentType.MARK_PRESENT, IntentType.MARK_PRESENT_MANY, IntentType.SET_SPEAKER, IntentType.RELEASE_SPEAKER,
    IntentType.READ_AGENDA, IntentType.NEXT_ITEM, IntentType.PREVIOUS_ITEM, IntentType.SKIP_ITEM,
    IntentType.ATTENDANCE, IntentType.SPEAKER_STATS, IntentType.SILENT_PARTICIPANTS, IntentType.TIME_STATUS,
    IntentType.OPEN_ACTIONS, IntentType.SUMMARIZE_PREVIOUS, IntentType.LIST_PARTICIPANTS,
})
# Yalnızca işaretlenir, önerilmez: önemli işlemler ve önceki göreve dokunan alan komutları
_FLAG_ONLY = frozenset(EXPLICIT_ONLY_INTENTS) | {IntentType.SET_ACTION_OWNER, IntentType.SET_ACTION_DUE,
                                                 IntentType.START_MEETING, IntentType.SEND_MINUTES,
                                                 IntentType.CREATE_MINUTES, IntentType.ANALYZE_MEETING,
                                                 IntentType.MARK_ABSENT, IntentType.REMOVE_PARTICIPANT,
                                                 IntentType.RENAME_PARTICIPANT,
                                                 IntentType.CREATE_INVITE, IntentType.ATTACH_INVITE,
                                                 IntentType.EXPORT_TASKS}
# Kayıt komutları (karar, görev, not, otopark, oylama, gündeme ekleme) hitapsızken SIRADAN KONUŞMA kalır:
# "karar verelim artık", "görev dağılımını sonra yaparız" gibi cümleler toplantının içeriğidir.
MAX_WORDS = 12
# Ayrıştırıcının tanımadığı ama asistana verilmiş kısa buyruk: sistem nesnesi + buyruk fiili
# ("toplantı özeti çıkar", "kişi ekle Ahmet", "tutanağı hazırla"). Yalnızca 8 sözcüğe kadar.
_SYSTEM_IMPERATIVE = re.compile(
    r"^(?:yeni\s+)?(?:bir\s+)?(?:kişi\w*|katılımcı\w*|görev\w*|karar\w*|gündem\w*|toplantı\w*|tutanağ\w*|tutanak\w*|"
    r"özet\w*|madde\w*|otopark\w*|oylama\w*|yoklama\w*)\b(?:\s+\S+){0,3}?\s+"
    r"(?:ekle|çıkar|oku|başlat|bitir|gönder|kaydet|yaz|aç|kapat|geç|özetle|oluştur|al|sil|göster)(?:\s+\S+){0,4}$")
_WORDS = re.compile(r"\w+")


@dataclass
class UnaddressedResult:
    command_like: bool
    intent: Intent | None = None  # onay önerilebilecek kurallı komut (yoksa yalnızca işaretlenir)


def classify_unaddressed(parser: CommandParser, text: str) -> UnaddressedResult:
    cleaned = (text or "").strip()
    words = len(_WORDS.findall(cleaned))
    if not cleaned or words > MAX_WORDS:
        return UnaddressedResult(False)
    intent = parser.parse_command(cleaned, raw_text=cleaned)
    if intent.type in PROPOSABLE:
        return UnaddressedResult(True, intent)
    if intent.type in _FLAG_ONLY:
        return UnaddressedResult(True)
    if intent.type == IntentType.UNKNOWN and intent.get("incomplete_intent") and words <= 3:
        return UnaddressedResult(True)  # "görev ekle" gibi içeriksiz kayıt komutu
    if intent.type != IntentType.UNKNOWN:
        return UnaddressedResult(False)
    lowered = " ".join(tr_lower(cleaned).strip(" .!?").split())
    if words <= 8 and _SYSTEM_IMPERATIVE.match(lowered):
        return UnaddressedResult(True)
    return UnaddressedResult(False)
