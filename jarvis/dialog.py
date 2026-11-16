"""Diyalog durum yöneticisi (Faz 5c.2): çok turlu, oturum içi ve kontrollü bağlam.

Ne tutulur?
  * `pending`  : onay bekleyen TEK işlem (mevcut PendingConfirmation). "evet" yalnızca bunu onaylar.
  * `question` : Jarvis'in sorduğu ve yanıt beklediği TEK soru (kişi seçimi, görev metni/sorumlu/tarih,
                 yarım kalan katılım cümlesi).
  * `context`  : son kişi, son konu, son niyet (yalnızca okuma amaçlı takip sorularında kullanılır).

Kurallar (güvenlik):
  * Durum JarvisAssistant örneğine aittir (oturumlar arası paylaşım yok) ve toplantıya bağlıdır; toplantı
    değişince sıfırlanır. Soru `timeout_s` sonra, onay mevcut LLM onay süresi sonra düşer.
  * "Jarvis" denmeden söylenen kısa yanıt YALNIZCA bekleyen bir soru/onay varsa, `unaddressed_window_s` içinde
    ve beklenen biçimdeyse kabul edilir (salt "evet", adaylardan biri, kısa görev metni...). Aksi halde cümle
    normal toplantı konuşmasıdır ve transkripte yazılır.
  * Bir soruya verilen yanıt hiçbir zaman tek başına kayıt yazmaz: sonuç her zaman bir onay önerisine dönüşür ve
    kayıt yalnızca "evet" ile, mevcut komut zincirinden (kurallı ayrıştırıcı → MeetingManager) yapılır.
  * Açık ve bağımsız yeni bir komut ("Jarvis, gündemi oku") bekleyen soruyu düşürür; engellenmez.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .utils.turkish_text import tr_lower

# Salt onay/ret (bağımsız sözcükler). "tamam" sohbet dolgusu olduğu için hitapsız onay sayılmaz.
_PURE_YES = re.compile(r"^(?:evet|onaylıyorum|onayla|evet onaylıyorum|evet lütfen|evet onayla|onay)$")
_PURE_NO = re.compile(r"^(?:hayır|vazgeç|vazgeçtim|iptal|iptal et|boş ver|hayır vazgeç|gerek yok|onaylama)$")
_FOLLOW_TIME = re.compile(r"\bne kadar(?: (?:süre|zaman))?(?:dır|dir)? (?:daha )?konuş\w*|\bkonuşma süre\w*"
                          r"|\bkaç dakika\w*(?: \w+)? konuş\w*")
_FOLLOW_OTHERS = re.compile(r"^(?:peki |ya |şimdi |bir de )?"
                            r"(?:diğer(?:leri|ler)?|öbürleri|geri kalan(?:lar)?|başkaları)"
                            r"(?: katılımcılar\w*| kişiler\w*)?(?: ne durumda| nasıl| ne kadar konuştu\w*)?$")
_WORDS = re.compile(r"\w+")


def normalize_answer(text: str) -> str:
    return " ".join(tr_lower(text or "").replace("’", "'").strip(" .,!?;:").split())


def is_pure_yes(text: str) -> bool:
    return bool(_PURE_YES.match(normalize_answer(text)))


def is_pure_no(text: str) -> bool:
    return bool(_PURE_NO.match(normalize_answer(text)))


def word_count(text: str) -> int:
    return len(_WORDS.findall(text or ""))


def follow_up_kind(text: str) -> str | None:
    """Bağlama dayanan kısa takip sorusu: "time" (ne kadar konuştu), "others" (peki diğerleri?)."""
    low = normalize_answer(text)
    if _FOLLOW_OTHERS.match(low):
        return "others"
    if _FOLLOW_TIME.search(low) and word_count(low) <= 8:
        return "time"
    return None


@dataclass
class Clarification:
    kind: str  # choose_person | record_text | task_owner | task_due | attendance_rest
    question: str
    created_at: float
    timeout_s: float
    meeting_id: str | None
    data: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0

    def expired(self, now: float) -> bool:
        return now - self.created_at > self.timeout_s


@dataclass
class DialogContext:
    last_person: str | None = None
    last_topic: str | None = None  # konuşma | katılım
    last_intent: str | None = None
    last_reply_at: float = float("-inf")


class DialogManager:
    MAX_ATTEMPTS = 2  # aynı soruya anlaşılmayan yanıt sayısı; aşılırsa soru düşer

    def __init__(self, timeout_s: float = 60.0, unaddressed_window_s: float = 20.0, enabled: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.timeout_s = timeout_s
        self.unaddressed_window_s = unaddressed_window_s
        self.enabled = enabled
        self.clock = clock
        self.pending = None  # PendingConfirmation | None
        self.question: Clarification | None = None
        self.context = DialogContext()
        self.last_spoken_at = float("-inf")  # asistanın son sesli yanıtının bittiği an
        self.meeting_id: str | None = None

    # ---------------------------------------------------------------- yaşam döngüsü
    def sync_meeting(self, meeting_id: str | None) -> bool:
        """Toplantı değiştiyse (yeni toplantı, kapanış) bekleyen her şey düşer. True: sıfırlandı."""
        if meeting_id == self.meeting_id:
            return False
        had_state = self.pending is not None or self.question is not None
        self.reset()
        self.meeting_id = meeting_id
        return had_state

    def reset(self) -> None:
        self.pending = None
        self.question = None
        self.context = DialogContext()

    def ask(self, kind: str, question: str, **data: Any) -> str:
        self.question = Clarification(kind, question, self.clock(), self.timeout_s, self.meeting_id, dict(data))
        return question

    def clear_question(self) -> None:
        self.question = None

    def active_question(self) -> Clarification | None:
        if self.question is not None and (self.question.expired(self.clock())
                                          or self.question.meeting_id != self.meeting_id):
            self.question = None
        return self.question

    def active_pending(self):
        """Süresi dolmamış onay. Süresi dolan kayıt burada silinmez: "Jarvis, evet" süre doldu bilgisini verir."""
        if self.pending is not None and self.pending.expired(self.clock()):
            return None
        return self.pending

    # ---------------------------------------------------------------- hitapsız yanıt penceresi
    def speech_ended(self) -> None:
        """Seslendirme bitti (TTS iş parçacığından çağrılır)."""
        self.last_spoken_at = self.clock()

    def within_unaddressed_window(self, created_at: float) -> bool:
        # Pencere, soru/yanıt seslendirilip BİTTİKTEN sonra başlar: uzun bir soru okunurken süre dolup kullanıcının
        # hitapsız yanıtı transkripte düşmesin (klavyede seslendirme olmadığından davranış değişmez).
        start = max(created_at, self.last_spoken_at)
        return self.clock() - start <= self.unaddressed_window_s

    def remember(self, person: str | None = None, topic: str | None = None, intent: str | None = None) -> None:
        if person:
            self.context.last_person = person
        if topic:
            self.context.last_topic = topic
        if intent:
            self.context.last_intent = intent

    def replied(self) -> None:
        self.context.last_reply_at = self.clock()

    def follow_up_allowed(self, addressed: bool) -> bool:
        return addressed or self.within_unaddressed_window(self.context.last_reply_at)
