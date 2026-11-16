"""Çok turlu diyalog akışı (Faz 5c.2): JarvisAssistant ile DialogManager arasındaki köprü.

Bu modül yalnızca SORU sorar, YANITI yorumlar ve sonunda bir ONAY ÖNERİSİ (PendingConfirmation) üretir.
Kayıt her zaman mevcut zincirden yapılır: kanonik komut → kurallı ayrıştırıcı → JarvisAssistant.handle_intent →
MeetingManager. LLM bu akışta kullanılmaz; hiçbir yanıt doğrudan veritabanına yazmaz.

Desteklenen akışlar:
  * Belirsiz kişi seçimi ("Selin Er mi, Selin Uçar mı?") → söz verme / yoklama / görev sorumlusu.
  * Eksik kayıt komutu ("görev ekle") → içerik → (gerekirse) sorumlu → (gerekirse) son tarih → onay.
  * Yarım katılım cümlesi ("Kerem ve Nihat geldi, Selin de...") → devamı → tüm liste onayı.
  * Bağlamlı okuma soruları ("Ne kadar süredir konuşuyor?", "Peki diğer katılımcılar?").
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import TYPE_CHECKING

from .dialog import Clarification, follow_up_kind, is_pure_no, is_pure_yes, normalize_answer, word_count
from .llm.intent_cues import heard_attendance_names, is_attendance_report
from .llm.router import PendingConfirmation, explicit_only_hint
from .llm.utterance_guard import dangling_fragment, is_negated, pre_llm_guard, security_guard
from .meeting.action_extractor import extract_action
from .utils.dates import find_date, format_tr_date
from .utils.turkish_text import (
    best_name_match,
    dative_suffix,
    locative_suffix,
    split_names,
    strip_honorifics,
    is_none_value,
    strip_name_suffix,
    tr_lower,
)
from .voice.command_parser import Intent, IntentType

if TYPE_CHECKING:  # pragma: no cover
    from .assistant import AssistantResponse, JarvisAssistant

log = logging.getLogger(__name__)

CONFIRM_HINT = "Onaylamak için «evet», vazgeçmek için «hayır» deyin."
OWNER_QUESTION = "Bu görevin sorumlusu kim? Yoksa «sorumlu yok» deyin."
DUE_QUESTION = "Son tarih var mı? Örneğin «cuma», «21 Ekim» veya «tarih yok»."
CANCELLED = "Tamam, vazgeçildi; hiçbir işlem yapılmadı."
SECURITY_CANCEL = "Bu yanıt bir işlem talimatı içeriyor; soru iptal edildi ve hiçbir işlem yapılmadı."
GAVE_UP = "Yanıtı anlayamadım; soru iptal edildi, hiçbir işlem yapılmadı. Lütfen komutu baştan söyleyin."
# İki alanlı yanıtta tarih kısmının başlangıcı: "sorumlu yok, son tarih yok", "Mehmet tarih cuma"
_DUE_MARKER = re.compile(r"[\s,;]+(?:ve\s+)?(?=(?:son\s+)?tarih\w*\b|son gün\w*\b)", re.IGNORECASE)
_ANSWER_FILLERS = re.compile(r"^(?:evet|hayır|şey|yani|o|olan|tabii|peki)\s+|\s+(?:olsun|olan|lütfen)$")
_NEGATIVE_REST = re.compile(r"\b(?:gelmedi|gelmeyecek|katılmadı|katılmayacak|gelemedi|yok|değil|hayır)\b")
_POSITIVE_REST = re.compile(r"\b(?:geldi|katıldı|burada|aramızda|evet|o da|gelmiş|katılmış)\b")
_RECORD_QUESTIONS = {
    IntentType.ADD_ACTION: ("Görevin içeriği nedir? Örneğin «Mehmet raporu cumaya kadar hazırlasın». "
                            "Vazgeçmek için «iptal» deyin."),
    IntentType.ADD_DECISION: "Kaydedilecek karar nedir? Vazgeçmek için «iptal» deyin.",
    IntentType.ADD_PARKING: "Otoparka alınacak konu nedir? Vazgeçmek için «iptal» deyin.",
}
# İçeriksiz kayıt isteği: "yeni bir görev ekleyelim", "bir karar yazalım" (metin yok → içerik sorulur)
_BARE_RECORD = re.compile(
    r"^(?:(?:şimdi|peki|bir de)\s+)?(?:yeni\s+)?(?:bir\s+)?(?P<kind>görev|aksiyon|karar|otopark konusu)(?:\s+daha)?\s+"
    r"(?:ekle|ekleyelim|ekler misin|ekleyebilir misin|oluştur|oluşturalım|oluşturur musun|aç|açalım|yaz|yazalım|"
    r"kaydet|kaydedelim|kaydeder misin|al|alalım)$")
_BARE_KIND = {"görev": IntentType.ADD_ACTION, "aksiyon": IntentType.ADD_ACTION, "karar": IntentType.ADD_DECISION,
              "otopark konusu": IntentType.ADD_PARKING}
_RECORD_PREFIX = {IntentType.ADD_DECISION: "karar", IntentType.ADD_PARKING: "otopark"}
_WEEKDAY_ANSWER = re.compile(r"^(?:bu |gelecek |önümüzdeki )?(?P<day>pazartesi|salı|çarşamba|perşembe|cuma|cumartesi|"
                             r"pazar)(?: günü)?$")
# Hitaplı yanıtta kurallı ayrıştırıcının başka komut sandığı ama soruya ait olan biçimler
_ANSWER_INTENTS = {
    "task_owner": (IntentType.SET_ACTION_OWNER,),
    "task_due": (IntentType.SET_ACTION_DUE,),
    "attendance_rest": (IntentType.MARK_PRESENT, IntentType.MARK_PRESENT_MANY),
}
_FIELD_PREFIX = re.compile(r"^\s*(?:sorumlu(?:su)?|son tarih(?:i)?|tarih(?:i)?)\s*(?:[:,\-–]\s*|\s+(?=\S))",
                           re.IGNORECASE)
_REASK = {
    "record_text": "İçeriği anlayamadım; lütfen kısaca söyleyin ya da «iptal» deyin.",
    "task_owner": ("Sorumlu katılımcı listesinde bulunamadı veya anlaşılamadı. Adı tekrar söyleyin veya «sorumlu yok» "
                   "deyin; tarihi de ekleyebilirsiniz: «Mehmet, son tarih cuma»."),
    "task_due": "Tarihi anlayamadım. Örneğin «cuma», «21 Ekim» deyin veya «tarih yok».",
}
# Hitapsız yanıtlarda kabul edilen en fazla sözcük sayısı (uzun cümle normal toplantı konuşmasıdır)
_MAX_WORDS = {"choose_person": 4, "record_text": 10, "task_owner": 7, "task_due": 6, "attendance_rest": 8}


_COUNT_WORDS = {2: "İki", 3: "Üç", 4: "Dört", 5: "Beş"}
_ORDINALS = {
    0: ("birinci", "ilk", "ilki", "birincisi", "1", "bir", "1.", "birinci olan", "ilk olan", "birincisini"),
    1: ("ikinci", "ikincisi", "2", "iki", "2.", "ikinci olan", "ikincisini"),
    2: ("üçüncü", "üçüncüsü", "3", "üç", "3.", "üçüncü olan"),
    3: ("dördüncü", "dördüncüsü", "4", "dört", "4."),
}
_LAST = ("sonuncu", "sonuncusu", "son", "sonuncu olan", "sonuncusunu")


def choice_question(candidates: list[str], query: str = "") -> str:
    """"İki Mehmet var: Mehmet Öztürk ve Mehmet Yılmaz. Hangisini kastediyorsunuz?" """
    listed = ", ".join(candidates[:-1]) + f" ve {candidates[-1]}"
    firsts = {tr_lower(c.split()[0]) for c in candidates if c.split()}
    count = _COUNT_WORDS.get(len(candidates), str(len(candidates)))
    if len(firsts) == 1:
        head = f"{count} {candidates[0].split()[0]} var: {listed}."
    else:
        spoken = " ".join(strip_name_suffix(w) for w in query.split())  # "Yılmaz'ı" → "Yılmaz"
        label = f"'{spoken}'" if spoken else "Bu ad"
        head = f"{label} {tr_lower(count)} kişiyle eşleşiyor: {listed}."
    return f"{head} Hangisini kastediyorsunuz? Tam adı, soyadı veya «birinci», «ikinci» diyebilirsiniz."


def _ordinal_choice(text: str, count: int) -> int | None:
    answer = normalize_answer(text)
    answer = re.sub(r"^(?:evet|tamam|şey)\s+", "", answer)
    answer = re.sub(r"\s+(?:kişi|olan|olanı|lütfen)$", "", answer)
    if answer in _LAST:
        return count - 1
    for index, forms in _ORDINALS.items():
        if answer in forms and index < count:
            return index
    return None


def _split_owner_due(text: str) -> tuple[str, str | None]:
    parts = _DUE_MARKER.split(text.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip(" ,;"):
        return parts[0].strip(" ,;"), parts[1]
    return text, None


class DialogFlow:
    def __init__(self, assistant: "JarvisAssistant"):
        self.a = assistant
        # Kişi seçiminden doğan onay: (onay nesnesi, seçim sorusu, seçilen ad). Onay beklerken seçimin
        # tekrarı ("Mehmet Öztürk", "birinci") diyaloğa aittir; transkripte konuşma olarak yazılmaz.
        self._choice: tuple | None = None

    @property
    def d(self):
        return self.a.dialog

    # ================================================================ giriş
    def handle(self, text: str) -> "AssistantResponse | None":
        """Bekleyen soru/onay veya bağlamlı takip sorusu varsa yanıtı üretir; değilse None (normal akış)."""
        if not self.d.enabled:
            return None
        addressed, command = self.a.parser.split_wake_word(text)
        question = self.d.active_question()
        pending = self.d.active_pending()
        if question is None and pending is None:
            return self._follow_up(command if addressed else text, addressed)
        if not addressed:
            return self._unaddressed(text, question, pending)
        parsed = self.a.parser.parse_command(command, raw_text=text)
        if question is None or parsed.type in (IntentType.REPEAT_LAST, IntentType.STOP_SPEECH):
            return None  # yalnızca onay bekliyor / son yanıtı tekrar: normal akışta (soru ve onay korunur)
        if parsed.type == IntentType.REJECT_PENDING:
            return self._cancel()
        if parsed.type == IntentType.CONFIRM and question.kind != "attendance_rest":
            return self._reply(f"Önce soruyu yanıtlayın: {question.question}", ok=False)
        if parsed.type in _ANSWER_INTENTS.get(question.kind, ()):
            # "Jarvis, sorumlu yok" / "Jarvis, son tarih cuma" / "Jarvis, evet Selin de geldi": soruya yanıttır;
            # önceki göreve veya tek başına yoklamaya uygulanmaz.
            return self._answer(question, command, addressed=True, field_answer=True)
        if parsed.type not in (IntentType.UNKNOWN, IntentType.CONFIRM):
            log.info("Bekleyen soru yeni komutla düşürüldü: %s", question.kind)
            self.d.clear_question()
            return None  # bağımsız yeni komut engellenmez
        return self._answer(question, command, addressed=True)

    def _unaddressed(self, text: str, question: Clarification | None, pending) -> "AssistantResponse | None":
        if question is None:
            if pending is not None and self.d.within_unaddressed_window(pending.created_at):
                if is_pure_yes(text):
                    return self.a.handle_intent(Intent(IntentType.CONFIRM, {}, text))
                if is_pure_no(text):
                    return self.a.handle_intent(Intent(IntentType.REJECT_PENDING, {}, text))
                repeated = self._repeated_choice(text, pending)
                if repeated is not None:
                    return repeated
            return None  # normal konuşma: transkripte gider, onay beklemeye devam eder
        if not self.d.within_unaddressed_window(question.created_at):
            return None
        if is_pure_no(text):
            return self._cancel()
        if word_count(text) > _MAX_WORDS.get(question.kind, 4):
            return None
        return self._answer(question, text, addressed=False)

    # ================================================================ soru açanlar
    def open_record(self, intent_type: IntentType) -> "AssistantResponse | None":
        if not self._can_ask() or intent_type not in _RECORD_QUESTIONS:
            return None
        question = self.d.ask("record_text", _RECORD_QUESTIONS[intent_type], intent=intent_type.value)
        return self._ask_reply(question)

    def open_bare_record(self, command: str) -> "AssistantResponse | None":
        match = _BARE_RECORD.match(normalize_answer(command))
        return self.open_record(_BARE_KIND[match.group("kind")]) if match else None

    def open_choose(self, candidates: list[str], template: str, query: str = "",
                    **data) -> "AssistantResponse | None":
        if not self._can_ask() or len(candidates) < 2:
            return None
        question = self.d.ask("choose_person", choice_question(candidates, query), candidates=list(candidates),
                              template=template, **data)
        return self._ask_reply(question)

    def open_attendance_rest(self, command: str) -> "AssistantResponse | None":
        """"Kerem ve Nihat geldi, Selin de..." → devamı sorulur; hiçbir kayıt yapılmaz."""
        fragment = dangling_fragment(command)
        if not self._can_ask() or fragment is None:
            return None
        cut = tr_lower(command).rstrip(" .!?…").rfind(fragment.split()[0])
        base_text = command[:cut].rstrip(" ,;") if cut > 0 else ""
        base = heard_attendance_names(base_text) if base_text and is_attendance_report(base_text) else None
        rest_words = [w for w in command[cut:].rstrip(" .!?…").split() if tr_lower(w) not in ("de", "da", "ve",
                                                                                                "ile", "bir")]
        if not base or not rest_words:
            return None
        person = strip_name_suffix(" ".join(rest_words))
        question = self.d.ask("attendance_rest",
                              f"Cümle yarım kaldı. Hiçbir kayıt yapılmadı. {person} de geldi mi? «Evet, {person} de "
                              f"geldi» veya «hayır» diyebilirsiniz.", base=base, person=person)
        return self._ask_reply(question)

    # ================================================================ yanıtlar
    def _answer(self, q: Clarification, text: str, addressed: bool,
                field_answer: bool = False) -> "AssistantResponse | None":
        """Yanıtı sorunun türüne göre yorumlar. Beklenen biçimde olmayan yanıt:
        hitapsızsa normal konuşmadır (None → transkript, soru bekler); hitaplı ve kısaysa soru tekrarlanır;
        hitaplı ve uzunsa bağımsız yeni bir komut sayılır (soru düşer, None → normal komut akışı)."""
        if security_guard(text) is not None or explicit_only_hint(text) is not None:
            self.d.clear_question()
            return self._reply(SECURITY_CANCEL, ok=False)
        if q.kind != "attendance_rest":
            if is_negated(text):
                return self._cancel()
            guard = pre_llm_guard(text)
            if guard is not None and guard.kind == "blocked":
                self.d.clear_question()
                return self._reply(SECURITY_CANCEL, ok=False)
        handler = {"choose_person": self._answer_choose, "record_text": self._answer_record,
                   "task_owner": self._answer_owner, "task_due": self._answer_due,
                   "attendance_rest": self._answer_attendance_rest}[q.kind]
        response = handler(q, text) if q.kind == "attendance_rest" or pre_llm_guard(text) is None else None
        if response is not None:
            return response
        if not addressed:
            return None
        # "Jarvis, sorumlu …" gibi soruya ait biçim (field_answer) anlaşılamasa bile yeni komut sayılmaz: aksi halde
        # SET_ACTION_OWNER olarak ÖNCEKİ göreve uygulanırdı (canlı test: "Yok Son Tarih Yok" sorumlusu).
        if word_count(text) > 3 and not field_answer:
            log.info("Soru (%s) bekleyen yanıt yerine yeni komut geldi; soru düşürüldü", q.kind)
            self.d.clear_question()
            return None
        return self._reask(q, _REASK.get(q.kind) or q.question)

    def _repeated_choice(self, text: str, pending) -> "AssistantResponse | None":
        """Kişi seçimi onay beklerken seçimin tekrarı/değişikliği. Seçim adaylarından birine karşılık gelmeyen
        metin None döner (normal konuşma olarak transkripte gider)."""
        if self._choice is None or self._choice[0] is not pending:
            return None
        _, question, chosen = self._choice
        name = self._choice_name(question, text)
        if name is None:
            return None
        if name == chosen:
            return self._reply(f"{chosen} seçildi; işlem onayınızı bekliyor. {CONFIRM_HINT}")
        return self._answer_choose(question, text)  # başka aday seçildi: öneri yeni seçimle yenilenir (onaya bağlı)

    def _choice_name(self, q: Clarification, text: str) -> str | None:
        candidates = q.data["candidates"]
        picked = _ordinal_choice(text, len(candidates))  # "birinci", "ikincisi", "sonuncusu", "2"
        if picked is not None:
            return candidates[picked]
        answer = strip_honorifics(_ANSWER_FILLERS.sub("", " ".join(text.strip(" .,!?").split())))
        answer = " ".join(strip_name_suffix(w) for w in answer.split())
        match = best_name_match(answer, candidates) if 0 < word_count(answer) <= 4 else None
        if match is None or not match.found:
            return None  # soyadı da iki adaya uyuyorsa yeniden sorulur; tahmin yapılmaz
        return match.name

    def _answer_choose(self, q: Clarification, text: str):
        name = self._choice_name(q, text)
        if name is None:
            return None
        response = self._apply_choice(q, name)
        pending = self.d.pending
        self._choice = (pending, q, name) if pending is not None else None
        return response

    def _apply_choice(self, q: Clarification, name: str):
        self.d.clear_question()
        template = q.data["template"]
        if template == "speaker":
            return self._propose(f"söz {locative_suffix(name)}", IntentType.SET_SPEAKER,
                                 f"{dative_suffix(name)} söz vermemi onaylıyor musunuz?", person=name)
        if template == "attendance":
            return self._propose_attendance([*q.data.get("others", []), name])
        if template == "last_owner":  # sorumlusu belirsiz kalan, az önce kaydedilmiş görev
            action = self.a.manager._last_action() if self.a.manager.has_active_meeting else None
            if action is None or action.id != q.data.get("action_id"):
                return self._reply("Görev listesi bu arada değişti; sorumlu atanmadı. 'Jarvis, sorumlu: tam ad' "
                                   "diyerek belirtin.", ok=False)
            return self._propose(f"sorumlu: {name}", IntentType.SET_ACTION_OWNER,
                                 f"'{action.text}' görevinin sorumlusu {name} olarak kaydedilsin mi?", person=name)
        if template == "participant":  # katılımcı düzeltmesi: seçilen adla aynı işlem yeniden kontrol edilir
            if q.data.get("which") == "new_name":
                return self.a.propose_participant(q.data["op"], q.data["name"], name)
            return self.a.propose_participant(q.data["op"], name, q.data.get("new_name", ""))
        task = dict(q.data["task"], owner=name, owner_done=True)
        return self._task_next(task)

    def _answer_record(self, q: Clarification, text: str):
        content = " ".join(text.strip(" .").split())
        intent_type = IntentType(q.data["intent"])
        if not content or word_count(content) < 2 or dangling_fragment(content) is not None \
                or is_attendance_report(content):
            return None
        self.d.clear_question()
        if intent_type == IntentType.ADD_ACTION:
            return self._task_next({"text": content})
        prefix = _RECORD_PREFIX[intent_type]
        label = "kararı" if intent_type == IntentType.ADD_DECISION else "otopark konusunu"
        return self._propose(f"{prefix}: {content}", intent_type,
                             f"Şu {label} kaydetmemi onaylıyor musunuz: «{content}»?")

    def _answer_owner(self, q: Clarification, text: str):
        """Sorumlu yanıtı. "sorumlu yok, son tarih yok" / "Mehmet, son tarih cuma" gibi iki alanı birden
        içeren yanıt iki alan olarak işlenir; karışık metin hiçbir zaman kişi adı sayılmaz."""
        task = dict(q.data["task"])
        owner_text, due_text = _split_owner_due(text)
        owner = self._parse_owner(owner_text)
        if owner is None:
            return None
        due = None
        if due_text is not None:
            due = self._parse_due(due_text)
            if due is None:
                return None
        if owner[0] == "ambiguous":
            self.d.clear_question()
            if due is not None:
                task.update(due=due[1], date_text=due[2], due_done=True)
            return self.open_choose(list(owner[1]), "task_owner", task=task)
        self.d.clear_question()
        task.update(owner=owner[1], owner_done=True)
        if due is not None:
            task.update(due=due[1], date_text=due[2], due_done=True)
        return self._task_next(task)

    def _answer_due(self, q: Clarification, text: str):
        due = self._parse_due(text)
        if due is None:
            return None
        self.d.clear_question()
        return self._task_next(dict(q.data["task"], due=due[1], date_text=due[2], due_done=True))

    def _parse_owner(self, text: str):
        """→ ("none", None) | ("name", ad) | ("ambiguous", adaylar) | None (anlaşılamadı)."""
        text = _FIELD_PREFIX.sub("", text)
        if is_none_value(text):
            return ("none", None)
        spoken = strip_honorifics(_ANSWER_FILLERS.sub("", " ".join(text.strip(" .,!?").split())))
        spoken = " ".join(strip_name_suffix(w) for w in spoken.split())
        if not 0 < word_count(spoken) <= 4:
            return None
        match = best_name_match(spoken, self._participants())
        if match.is_ambiguous:
            return ("ambiguous", match.candidates)
        return ("name", match.name) if match.found else None

    def _parse_due(self, text: str):
        """→ ("none", None, "") | ("date", iso, ifade) | None (anlaşılamadı)."""
        text = _FIELD_PREFIX.sub("", text)
        if is_none_value(text):
            return ("none", None, "")
        answer = normalize_answer(text)
        if not answer or word_count(answer) > 5:
            return None
        weekday = _WEEKDAY_ANSWER.match(answer)
        if weekday:  # yalın gün adı ("cuma") bu soruda bir son tarihtir → "cumaya kadar"
            text = f"{dative_suffix(weekday.group('day')).replace(chr(39), '')} kadar"
        found = find_date(text, self._meeting_date())
        return ("date", found.value.isoformat(), found.text) if found else None

    def _answer_attendance_rest(self, q: Clarification, text: str):
        low = normalize_answer(text)
        base = list(q.data["base"])
        if low in ("evet", "evet geldi", "geldi", "o da geldi", "evet o da geldi"):
            extra = [q.data["person"]]
        elif _NEGATIVE_REST.search(low) and not _POSITIVE_REST.search(low.replace("hayır", "")):
            extra = []
        elif _POSITIVE_REST.search(low):
            spoken = re.split(r"\s+(?:de|da)?\s*(?:geldi|katıldı|burada|aramızda|gelmiş|katılmış)\b", text,
                              flags=re.IGNORECASE)[0]
            spoken = re.sub(r"^\s*(?:evet|tabii|o da)\s*[,]?\s*", "", spoken, flags=re.IGNORECASE)
            extra = [strip_name_suffix(n) for n in split_names(spoken)] or [q.data["person"]]
        else:
            return None
        self.d.clear_question()
        return self._propose_attendance(base + extra)

    # ================================================================ görev akışı
    def _task_next(self, task: dict) -> "AssistantResponse":
        participants = self._participants()
        if "parsed" not in task:
            draft = extract_action(task["text"], participants, self._meeting_date())
            task["parsed"] = True
            if draft.owner:
                task.update(owner=draft.owner, owner_done=True)
            elif draft.owner_ambiguous:
                return self.open_choose(list(draft.owner_candidates), "task_owner", task=task) or \
                    self._reply("Görevin sorumlusu belirsiz; lütfen görevi tam adla söyleyin.", ok=False)
            if draft.due_date:
                task.update(due=draft.due_date.isoformat(), date_text=draft.date_text, due_done=True)
        if not task.get("owner_done"):
            return self._ask_reply(self.d.ask("task_owner", OWNER_QUESTION, task=task))
        if not task.get("due_done"):
            return self._ask_reply(self.d.ask("task_due", DUE_QUESTION, task=task))
        return self._propose_task(task)

    def _propose_task(self, task: dict) -> "AssistantResponse":
        participants = self._participants()
        text = task["text"]
        due = date.fromisoformat(task["due"]) if task.get("due") else None
        owner = task.get("owner")
        canonical_text = text
        base = extract_action(text, participants, self._meeting_date())
        if due and base.due_date != due:
            canonical_text = f"{canonical_text} {task['date_text']}"
        if owner and base.owner != owner:
            canonical_text = f"{canonical_text}, sorumlu: {owner}"
        check = extract_action(canonical_text, participants, self._meeting_date())
        intent = self.a.parser.parse_command(f"görev: {canonical_text}")
        owner_ok = (check.owner or None) == (owner or None)
        if not owner_ok or check.due_date != due or intent.type != IntentType.ADD_ACTION:
            return self._reply("Görevi güvenle oluşturamadım; hiçbir kayıt yapılmadı. Lütfen tek cümlede söyleyin "
                               "(ör. «Jarvis, görev: Mehmet raporu cumaya kadar hazırlasın»).", ok=False)
        intent.params["owner_none"] = owner is None
        intent.params["due_none"] = due is None
        summary = (f"Görev: {check.description}. Sorumlu: {owner or 'belirtilmedi'}. Son tarih: "
                   f"{format_tr_date(due) if due else 'belirtilmedi'}. Kaydetmemi onaylıyor musunuz?")
        return self._pending(intent, f"görev: {canonical_text}", summary, person=owner)

    # ================================================================ ortak
    def _propose_attendance(self, names: list[str]) -> "AssistantResponse":
        participants = self._participants()
        resolved: list[str] = []
        new: list[str] = []
        for spoken in dict.fromkeys(n.strip() for n in names if n and n.strip()):
            match = best_name_match(spoken, participants)
            if match.is_ambiguous:
                others = [n for n in names if n != spoken]
                return self.open_choose(list(match.candidates), "attendance", others=others, query=spoken) or \
                    self._reply("Bir ad birden çok kişiyle eşleşiyor; lütfen listeyi tam adlarla söyleyin.", ok=False)
            name = match.name if match.found else spoken
            if name in resolved:
                continue  # seçim sonrası aynı kişi iki kez listelenmez
            if not match.found:
                new.append(name)
            resolved.append(name)
        if not resolved:
            return self._reply(CANCELLED)
        listed = ", ".join(resolved[:-1]) + (f" ve {resolved[-1]}" if len(resolved) > 1 else resolved[0])
        note = f" Listede olmayan, yeni eklenecek: {', '.join(new)}." if new else ""
        intent = self.a.parser.parse_command(f"katılımcılar: {', '.join(resolved)}")
        intent.params["confirmed_new"] = True  # tüm liste kullanıcıya gösterildi; onayla bir bütün olarak işlenir
        return self._pending(intent, f"katılımcılar: {', '.join(resolved)}",
                             f"Şu kişilerin yoklamasını onaylıyor musunuz: {listed}?{note}")

    def _propose(self, canonical: str, expected: IntentType, message: str, person: str | None = None):
        intent = self.a.parser.parse_command(canonical)
        if intent.type != expected:  # kurallı ayrıştırıcı doğrulamazsa öneri yapılmaz
            return self._reply("Bu yanıtla güvenli bir komut oluşturamadım; hiçbir işlem yapılmadı.", ok=False)
        return self._pending(intent, canonical, message, person=person)

    def _pending(self, intent: Intent, canonical: str, message: str, person: str | None = None):
        intent.params["via_dialog"] = True
        self.d.pending = PendingConfirmation(intent, canonical, self.d.clock(), self.a.llm.confirm_timeout_s)
        self.d.remember(person=person)
        return self._reply(f"{message} {CONFIRM_HINT}")

    def _cancel(self) -> "AssistantResponse":
        self.d.clear_question()
        self.d.pending = None
        return self._reply(CANCELLED)

    def _reask(self, q: Clarification, message: str):
        q.attempts += 1
        if q.attempts > self.d.MAX_ATTEMPTS:
            self.d.clear_question()
            return self._reply(GAVE_UP, ok=False)
        return self._reply(message, ok=False)

    def _reply(self, message: str, ok: bool = True, intent: IntentType = IntentType.UNKNOWN) -> "AssistantResponse":
        from .assistant import AssistantResponse  # noqa: PLC0415

        return AssistantResponse(message, intent, ok=ok)

    def _ask_reply(self, question: str) -> "AssistantResponse":
        """Soru: henüz hiçbir işlem yapılmadı (ok=False, eski açıklama isteği yanıtlarıyla aynı)."""
        return self._reply(question, ok=False)

    def _can_ask(self) -> bool:
        return self.d.enabled and self.a.manager.has_active_meeting

    def _participants(self) -> list[str]:
        """Eşleştirme için tam adlar: toplantı listesi + rehberde olup listede olmayanlar."""
        return self.a.manager.known_names() if self.a.manager.has_active_meeting else []

    def _meeting_date(self) -> date:
        meeting = self.a.manager.meeting if self.a.manager.has_active_meeting else None
        return meeting.started_at.date() if meeting else date.today()

    # ================================================================ bağlamlı okuma soruları
    def _follow_up(self, text: str, addressed: bool) -> "AssistantResponse | None":
        kind = follow_up_kind(text)
        if kind is None or not self.a.manager.has_active_meeting or not self.d.follow_up_allowed(addressed):
            return None
        if addressed and self.a.parser.parse_command(text).type != IntentType.UNKNOWN:
            return None  # açık komut normal akışta
        context = self.d.context
        if kind == "time":
            person = self._named_person(text) or context.last_person or self.a.manager.meeting.active_speaker
            if person is None:
                return self._reply("Kimin konuşma süresini soruyorsunuz? Lütfen adını söyleyin.", ok=False)
            result = self.a.manager.speaker_time(person)
            self.d.remember(person=person, topic="konuşma", intent="speaker_time")
            return self._reply(result.message, intent=IntentType.SPEAKER_STATS)
        if context.last_topic == "konuşma" and context.last_person:
            result = self.a.manager.speaker_time(context.last_person, others=True)
            return self._reply(result.message, intent=IntentType.SPEAKER_STATS)
        if context.last_topic == "katılım":
            return self._reply(self.a.manager.attendance_status().message, intent=IntentType.ATTENDANCE)
        return self._reply("Hangi konuda diğerlerini soruyorsunuz? Örneğin «Jarvis, konuşma süreleri».", ok=False)

    def _named_person(self, text: str) -> str | None:
        participants = self._participants()
        words = [strip_name_suffix(w.strip(" ,.?!")) for w in text.split()]
        for size in (2, 1):
            for i in range(len(words) - size + 1):
                chunk = " ".join(words[i:i + size])
                if not chunk[:1].isupper():
                    continue
                match = best_name_match(chunk, participants)
                if match.found:
                    return match.name
        return None
