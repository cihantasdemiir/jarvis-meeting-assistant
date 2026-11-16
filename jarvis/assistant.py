"""JarvisAssistant: orkestrasyon katmanı.

Girdiyi alır, komut mu konuşma mı olduğuna karar verir, komutu
MeetingManager'a yönlendirir ve sonucu SpeechEngine ile bildirir.
İş kuralları burada değil, MeetingManager'dadır.

Süre uyarıları (G-12) iki yoldan gelir:
  * her girdiden sonra kontrol edilir,
  * TimeboxMonitor arka plan iş parçacığı belirli aralıklarla kontrol eder
    (klavye modunda kullanıcı yazmıyorken de uyarı verilebilsin diye).
MeetingManager'a erişim tek bir kilitle korunur.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .diagnostics import PipelineTrace
from .dialog import DialogManager
from .dialog_flow import DialogFlow
from .meeting.manager import MeetingManager, NoActiveMeeting, OperationResult
from .meeting.models import Meeting, TranscriptKind
from .meeting.storage import StorageError
from .llm.analyzer import candidates_to_drafts
from .llm.history_qa import answer_question
from .llm.router import EXPLICIT_ONLY_INTENTS, PendingConfirmation, explicit_only_hint
from .llm.utterance_guard import pre_llm_guard
from .llm.services import LlmServices, disabled_services
from .reports.minutes_service import MinutesService
from .voice.command_parser import HELP_TEXT, CommandParser, Intent, IntentType
from .voice.listener import InputSource
from .voice.speech_engine import SpeechEngine
from .voice.truncation import detect_truncation
from .voice.unaddressed import classify_unaddressed

log = logging.getLogger(__name__)

# Henüz uygulanmamış komutlar: tanınır ama çalışıyor gibi gösterilmez.
_PLANNED_INTENTS: dict[IntentType, str] = {}
_PARTICIPANT_OPS = (IntentType.MARK_ABSENT, IntentType.REMOVE_PARTICIPANT, IntentType.RENAME_PARTICIPANT)

TIMEBOX_CHECK_SECONDS = 15.0


@dataclass
class AssistantResponse:
    message: str
    intent: IntentType | None = None
    ok: bool = True
    stop: bool = False
    is_command: bool = True
    speak: bool = True  # False: yalnızca konsola yazılır (ör. "sesli yanıt durduruldu")


class TimeboxMonitor(threading.Thread):
    """Gündem süresini arka planda izler ve %80 uyarısını seslendirir."""

    def __init__(self, assistant: "JarvisAssistant", interval: float = TIMEBOX_CHECK_SECONDS):
        super().__init__(daemon=True, name="timebox-monitor")
        self.assistant = assistant
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                message = self.assistant.poll_timebox()
                if message:
                    self.assistant.speech.say(message)
            except Exception:  # izleyici hiçbir koşulda ana programı düşürmemeli
                log.exception("Süre izleyici hatası")

    def stop(self) -> None:
        self._stop_event.set()


class JarvisAssistant:
    def __init__(self, manager: MeetingManager, parser: CommandParser, speech: SpeechEngine,
                 require_wake: bool = True, minutes: MinutesService | None = None,
                 llm: LlmServices | None = None, background_llm: bool | None = None,
                 dialog: DialogManager | None = None, propose_unaddressed: bool = True,
                 trace: PipelineTrace | None = None):
        # Faz 5c.2: oturum içi diyalog durumu (bekleyen onay, bekleyen soru, son kişi/konu). Örneğe özeldir.
        self.dialog = dialog or DialogManager()
        # Hitapsız ama komut kalıbındaki cümle için onay önerisi (UNADDRESSED_COMMAND_PROMPT); kapalıysa yalnızca işaret
        self.propose_unaddressed = propose_unaddressed
        self.trace = trace or PipelineTrace(None)  # girdi zinciri tanılaması (metinsiz)
        self.last_reply = ""  # "son söylediğini tekrar et" için asistanın son yanıtı (yalnızca bu oturumda)
        self.manager = manager
        self.llm = llm or disabled_services()
        # LLM ile analiz/soru-cevap uzun sürebilir: varsayılan olarak arka planda çalışır (toplantı akışı durmaz).
        self.background_llm = self.llm.enabled if background_llm is None else background_llm
        self._llm_job: threading.Thread | None = None
        self.minutes = minutes
        self.parser = parser
        self.speech = speech
        if hasattr(speech, "on_speech_end") and speech.on_speech_end is None:
            # Hitapsız yanıt penceresi, yanıtın seslendirilmesi bittikten sonra başlar (sesli giriş = klavye).
            speech.on_speech_end = self.dialog.speech_ended
        self.require_wake = require_wake
        self._lock = threading.RLock()
        self.flow = DialogFlow(self)
        m = self.manager
        self._handlers: dict[IntentType, Callable[[Intent], OperationResult]] = {
            IntentType.START_MEETING: lambda i: m.start_meeting(i.get("title")),
            IntentType.END_MEETING: lambda i: self._end_meeting(),
            IntentType.ATTENDANCE: lambda i: m.attendance_status(),
            IntentType.MARK_PRESENT: self._mark_present,
            IntentType.MARK_PRESENT_MANY: self._mark_present,
            IntentType.READ_AGENDA: lambda i: m.read_agenda(),
            IntentType.NEXT_ITEM: lambda i: m.next_item(),
            IntentType.PREVIOUS_ITEM: lambda i: m.previous_item(),
            IntentType.SKIP_ITEM: lambda i: m.skip_item(),
            IntentType.ADD_AGENDA_ITEM: lambda i: m.add_agenda_item(i.get("title", ""), i.get("minutes")),
            IntentType.SUMMARIZE_PREVIOUS: lambda i: m.summarize_previous(),
            IntentType.OPEN_ACTIONS: lambda i: m.open_actions(),
            IntentType.SET_SPEAKER: lambda i: m.set_speaker(i.get("speaker", "")),
            IntentType.RELEASE_SPEAKER: lambda i: m.release_speaker(),
            IntentType.SILENT_PARTICIPANTS: lambda i: m.silent_participants(),
            IntentType.SPEAKER_STATS: lambda i: m.speaker_stats(),
            IntentType.ADD_DECISION: lambda i: m.add_decision(i.get("text", "")),
            IntentType.ADD_ACTION: lambda i: m.add_action(i.get("text", ""), bool(i.get("owner_none")),
                                                          bool(i.get("due_none"))),
            IntentType.COMPLETE_ACTION: lambda i: m.complete_action(i.get("query", "")),
            IntentType.CANCEL_ACTION: lambda i: m.cancel_action(i.get("query", "")),
            IntentType.SET_ACTION_OWNER: lambda i: m.set_last_action_owner(i.get("name", "")),
            IntentType.SET_ACTION_DUE: lambda i: m.set_last_action_due(i.get("date_text", "")),
            IntentType.UNDO_LAST: lambda i: m.undo_last(),
            IntentType.ADD_NOTE: lambda i: m.add_note(i.get("text", "")),
            IntentType.ADD_PARKING: lambda i: m.add_parking(i.get("text", "")),
            IntentType.VOTE: lambda i: m.add_vote(i.get("topic", ""), i.get("yes"), i.get("no"), i.get("abstain")),
            IntentType.TIME_STATUS: lambda i: m.time_status(),
            IntentType.NEXT_MEETING: self._next_meeting,
            IntentType.CREATE_INVITE: lambda i: self._invite(attach=False),
            IntentType.ATTACH_INVITE: lambda i: self._invite(attach=True),
            IntentType.EXPORT_TASKS: lambda i: self._export_tasks(str(i.get("scope", "meeting"))),
            IntentType.SEND_MINUTES: lambda i: self._send_minutes(bool(i.get("force"))),
            IntentType.CREATE_MINUTES: lambda i: self._create_minutes(),
            IntentType.CONFIRM: lambda i: self._confirm_pending(),
            IntentType.REJECT_PENDING: lambda i: self._reject_pending(),
            IntentType.ANALYZE_MEETING: lambda i: self._start_analysis(),
            IntentType.LIST_DRAFTS: lambda i: m.list_drafts(),
            IntentType.APPROVE_DRAFT: lambda i: m.approve_draft(i.get("number")),
            IntentType.REJECT_DRAFT: lambda i: m.reject_draft(i.get("number")),
            IntentType.READ_AI_SUMMARY: lambda i: m.read_ai_summary(),
            IntentType.ASK_HISTORY: lambda i: self._ask_history(i.get("question", "")),
            IntentType.MARK_ABSENT: lambda i: self._participant_change(i, "absent"),
            IntentType.REMOVE_PARTICIPANT: lambda i: self._participant_change(i, "remove"),
            IntentType.RENAME_PARTICIPANT: lambda i: self._participant_change(i, "rename"),
            IntentType.LIST_PARTICIPANTS: lambda i: m.participant_roster(),
        }

    @property
    def pending(self) -> PendingConfirmation | None:
        """Onay bekleyen TEK işlem (diyalog yöneticisinde tutulur; "evet" yalnızca bunu onaylar)."""
        return self.dialog.pending

    @pending.setter
    def pending(self, value: PendingConfirmation | None) -> None:
        self.dialog.pending = value

    def _new_pending(self, intent: Intent, canonical: str) -> None:
        self.dialog.clear_question()  # yeni onay önerisi eski soruyu geçersiz kılar
        self.pending = PendingConfirmation(intent, canonical, self.dialog.clock(), self.llm.confirm_timeout_s)

    # ---------------------------------------------------------------- tek girdi
    def handle_text(self, text: str) -> AssistantResponse:
        """Bir girdi satırını işler; yanıtı konuşmadan döndürür.

        `text` bir Utterance ise (sesli giriş) transkript zamanı olarak konuşmanın
        söylendiği an kullanılır, tanındığı an değil.
        """
        spoken_at: datetime | None = getattr(text, "captured_at", None)
        duration_s = _utterance_seconds(text)
        self.trace.begin(text)
        text = (text or "").strip()
        if not text:
            self.trace.current = None
            return AssistantResponse("", is_command=False)
        intent: Intent | None = None
        with self._lock:
            self.dialog.sync_meeting(self.manager.meeting.id if self.manager.has_active_meeting else None)
            # Önce bekleyen soru/onay ve bağlamlı takip soruları değerlendirilir (Faz 5c.2). Diyaloğun tükettiği
            # yanıt transkripte yazılmaz; diyalog ilgilenmezse (None) eski akış aynen çalışır.
            response = self.flow.handle(text)
            if response is not None:
                self.trace.note(asama="diyalog", hitap="hitaplı" if self.parser.is_addressed(text) else "hitapsız")
            else:
                intent = self.parser.parse(text, require_wake=self.require_wake)
                if intent is None:
                    self.trace.note(hitap="hitapsız", asama="transkript")
                    response = self._handle_unaddressed(text, spoken_at, duration_s)
                else:
                    log.info("Komut: %s -> %s %s", text, intent.type.value, intent.params)
                    self.trace.note(hitap="hitaplı", asama="ayrıştırıcı", ayristirici=intent.type.value,
                                    ayristirici_neden=_parser_reason(intent))
        if intent is not None:
            # UNKNOWN komutta LLM çağrısı kilit dışında yapılır (süre uyarıları beklemesin).
            response = self.handle_intent(intent)
        assert response is not None
        if response.is_command and response.message:
            self.dialog.replied()
        with self._lock:
            warning = self.poll_timebox()
        if warning:
            response.message = f"{response.message} {warning}".strip()
        if response.is_command and response.message and response.intent not in (IntentType.REPEAT_LAST,
                                                                                 IntentType.STOP_SPEECH):
            self.last_reply = response.message
        self.trace.finish(response)
        return response

    def handle_intent(self, intent: Intent) -> AssistantResponse:
        if intent.type == IntentType.STOP_SPEECH:  # yalnızca ses kesilir: onay, soru ve kayıtlar olduğu gibi kalır
            self.stop_speaking()
            return AssistantResponse("Sesli yanıt durduruldu; dinliyorum.", intent.type, speak=False)
        if intent.type == IntentType.REPEAT_LAST:  # yalnızca okur: bekleyen onay ve soru korunur
            if not self.last_reply:
                return AssistantResponse("Henüz bir yanıt vermedim.", intent.type, ok=False)
            return AssistantResponse(f"Son söylediğim: {self.last_reply}", intent.type)
        if intent.type not in (IntentType.CONFIRM, IntentType.REJECT_PENDING) and self.pending is not None:
            # Onay bekleyen öneri yalnızca hemen ardından gelen "evet" ile çalışır; başka komut öneriyi düşürür.
            log.info("Bekleyen LLM önerisi düşürüldü: %s", self.pending.canonical)
            self.pending = None
        if intent.type == IntentType.HELP:
            return AssistantResponse(HELP_TEXT, intent.type)
        if intent.type == IntentType.EXIT:
            return AssistantResponse(self._exit_message(), intent.type, stop=True)
        if intent.type == IntentType.UNKNOWN:
            if intent.get("stt_suggestion"):  # STT bozulması olasılığı: çalıştırılmaz, onay sorulur
                return self._propose_stt_fix(str(intent.get("stt_suggestion")), str(intent.get("command", "")))
            if intent.get("clarify"):  # kayıt komutunun metni yok/ertelenmiş: kayıt yapılmaz, LLM'ye gidilmez
                if intent.get("incomplete_intent"):  # Faz 5c.2: eksik metin sorulur (yanıt yine onaya gider)
                    opened = self.flow.open_record(IntentType(str(intent.get("incomplete_intent"))))
                    if opened is not None:
                        return opened
                return AssistantResponse(str(intent.get("clarify")), intent.type, ok=False)
            truncation = detect_truncation(str(intent.get("command", "")))
            if truncation is not None:
                # Kesik komut tahminle tamamlanmaz ve LLM'ye de gönderilmez (yanlış işlem riski).
                log.info("Kesik komut: %s", intent.raw_text)
                return AssistantResponse(truncation.message(), intent.type, ok=False)
            return self._route_unknown(intent)
        if intent.type in _PLANNED_INTENTS:
            phase = _PLANNED_INTENTS[intent.type]
            return AssistantResponse(f"Bu komut tanındı ancak henüz uygulanmadı ({phase}).",
                                     intent.type, ok=False)
        handler = self._handlers.get(intent.type)
        if handler is None:
            return AssistantResponse("Bu komut için işlem tanımlı değil.", intent.type, ok=False)
        with self._lock:
            try:
                result = handler(intent)
            except NoActiveMeeting as exc:
                return AssistantResponse(str(exc), intent.type, ok=False)
            except StorageError as exc:
                log.exception("Kayıt hatası")
                return AssistantResponse(self._storage_error_message(exc), intent.type, ok=False)
            follow = self._after_handler(intent, result)
        if follow is not None:
            return follow
        return AssistantResponse(result.message, intent.type, ok=result.ok)

    def _after_handler(self, intent: Intent, result: OperationResult) -> AssistantResponse | None:
        """Faz 5c.2: sonuca göre bağlamı günceller veya belirsiz kişide seçim sorusu açar (kayıt yapmaz)."""
        kind = intent.type
        if kind in _PARTICIPANT_OPS and not result.ok and result.data.get("candidates"):
            data = result.data
            which = "new_name" if data.get("name") else "name"  # rename'de hedef ad da belirsiz olabilir
            return self.flow.open_choose(list(data["candidates"]), "participant", op=data["op"], which=which,
                                         name=data.get("name") or "", new_name=data.get("new_name") or "",
                                         query=data.get("query") or "")
        if kind == IntentType.SET_SPEAKER:
            candidates = result.data.get("ambiguous") if result.data else None
            if not result.ok and candidates:
                return self.flow.open_choose(list(candidates), "speaker", query=str(intent.get("speaker", "")))
            if result.ok and self.manager.has_active_meeting:
                self.dialog.remember(person=self.manager.meeting.active_speaker, topic="konuşma",
                                     intent=kind.value)
        elif kind == IntentType.ADD_ACTION and result.ok and result.data and result.data.get("owner_candidates"):
            # Görev sorumlusuz kaydedildi; hangi kişinin kastedildiği sorulur. Yanıt yine onaya bağlıdır.
            opened = self.flow.open_choose(list(result.data["owner_candidates"]), "last_owner",
                                           action_id=result.data.get("action_id", ""))
            if opened is not None:
                hint = "'Jarvis, sorumlu: tam ad' diyerek belirtin."
                return AssistantResponse(f"{result.message.replace(hint, '').strip()} {opened.message}", kind,
                                         ok=True)
        elif kind in (IntentType.SPEAKER_STATS, IntentType.SILENT_PARTICIPANTS, IntentType.RELEASE_SPEAKER):
            self.dialog.remember(topic="konuşma", intent=kind.value)
        elif kind in (IntentType.ATTENDANCE, IntentType.MARK_PRESENT, IntentType.MARK_PRESENT_MANY):
            ambiguous = result.data.get("ambiguous") if result.data else None
            if not result.ok and ambiguous and not result.data.get("uncertain"):
                # Belirsiz adlar sırayla sorulur; çözülene kadar listedeki HİÇ KİMSE işaretlenmez.
                spoken, candidates = next(iter(ambiguous.items()))
                others = [*result.data.get("accepted", []), *[n for n in ambiguous if n != spoken]]
                opened = self.flow.open_choose(list(candidates), "attendance", others=others, query=spoken)
                if opened is not None:
                    return opened
            self.dialog.remember(topic="katılım", intent=kind.value)
        return None

    @staticmethod
    def _storage_error_message(exc: Exception) -> str:
        return (f"Kayıt hatası: {exc}. Son işlem bellekte tutuluyor; bir sonraki komutta yeniden "
                "kaydedilmeye çalışılacak. Sorun sürerse diskin dolu veya dosyanın kilitli olmadığını kontrol edin.")

    def _handle_unaddressed(self, text: str, spoken_at: datetime | None,
                            duration_s: float | None = None) -> AssistantResponse:
        """Hitapla başlamayan metin. Ortasında "Jarvis ..." komutu varsa önceki kısım
        transkripte yazılır ve komut çalıştırılır; anlaşılmayan gömülü komutta metnin
        tamamı konuşma sayılır (konuşma kaybolmaz, yanlış komut çalışmaz)."""
        near = self.parser.split_near_miss(text)
        if near is not None:
            # Yakın hitap ("Charles ..."): konuşma kaybolmasın diye transkripte yazılır; komut ÇALIŞMAZ,
            # yalnızca geçerli bir komuta karşılık geliyorsa onay sorulur (WAKE_WORD_NEAR_MISSES, varsayılan kapalı).
            word, rest = near
            self.trace.note(hitap="yakın_hitap")
            speech = self._handle_speech(text, spoken_at, duration_s, kind=TranscriptKind.COMMAND_LIKE)
            intent = self.parser.parse_command(rest, raw_text=text)
            if intent.type == IntentType.UNKNOWN and intent.get("stt_suggestion"):
                rest = str(intent.get("stt_suggestion"))
                intent = self.parser.parse_command(rest, raw_text=text)
            if intent.type == IntentType.UNKNOWN:
                return speech
            log.info("Yakın hitap '%s': onay önerisi %s", word, intent.type.value)
            return self._propose_stt_fix(rest, text, heard_wake=word)
        split = self.parser.split_embedded_command(text)
        if split is not None:
            before, command_text = split
            intent = self.parser.parse(command_text)
            if intent is not None and intent.type != IntentType.UNKNOWN:
                self.trace.note(hitap="gömülü", ayristirici=intent.type.value)
                if before:
                    self._handle_speech(before, spoken_at)
                log.info("Gömülü komut: %s -> %s %s", command_text, intent.type.value, intent.params)
                return self.handle_intent(intent)
        return self._handle_speech(text, spoken_at, duration_s, classify=True)

    def _handle_speech(self, text: str, spoken_at: datetime | None = None, duration_s: float | None = None,
                       kind: str = "", classify: bool = False, propose: bool = True) -> AssistantResponse:
        """Komut olmayan konuşma: aktif toplantı varsa transkripte yazılır.

        `classify=True`: hitapsız cümle komut kalıbındaysa (STT "Jarvis"i düşürmüş olabilir) kayıt silinmeden
        "komut gibi duyuldu" diye işaretlenir; analizde ve konuşma istatistiğinde konuşma sayılmaz. Önemli olmayan
        bir komutsa onay önerilir; hiçbir zaman kendiliğinden çalıştırılmaz."""
        if not self.manager.has_active_meeting:
            return AssistantResponse("", is_command=False, ok=False)
        candidate = None
        if classify and not kind:
            result = classify_unaddressed(self.parser, text)
            if result.command_like:
                self.trace.note(olasi_niyet=result.intent.type.value if result.intent else "kalıp")
                kind = TranscriptKind.COMMAND_LIKE
                candidate = result.intent
                log.info("Hitapsız komut kalıbı işaretlendi (%s)", candidate.type.value if candidate else "-")
        if kind:
            self.trace.note(transkript="komut_benzeri")
        try:
            stored = self.manager.add_transcript(text, at=spoken_at, kind=kind, duration_s=duration_s)
        except StorageError as exc:
            log.exception("Transkript kaydedilemedi")
            return AssistantResponse(self._storage_error_message(exc), is_command=False, ok=False)
        busy = self.dialog.active_question() is not None or self.dialog.active_pending() is not None
        if candidate is None or not propose or not self.propose_unaddressed or busy or not self.dialog.enabled:
            return AssistantResponse("", is_command=False)
        candidate.params["transcript_index"] = stored.data.get("index")
        candidate.params["transcript_text"] = stored.data.get("text", text)
        self._new_pending(candidate, text)
        return AssistantResponse(
            f"«{text}» bir komut gibi duyuldu ama «Jarvis» duyulmadı; transkriptte komut olarak işaretlendi. "
            "Uygulamamı istiyorsanız «evet» deyin; istemiyorsanız devam edebilirsiniz.", IntentType.UNKNOWN, ok=True)

    def poll_timebox(self) -> str | None:
        """%80 süre uyarısı varsa metnini döndürür (en fazla bir kez)."""
        with self._lock:
            try:
                result = self.manager.check_timebox()
            except StorageError:
                log.exception("Süre uyarısı kaydedilemedi")
                return None
        return result.message if result else None

    def _propose_stt_fix(self, fixed: str, heard: str, heard_wake: str = "") -> AssistantResponse:
        """Ses tanıma bozulması olası komutu ÖNERİR (Faz 5a.3). Kayıt yalnızca "Jarvis, evet" ile yapılır;
        önemli işlemler (bitir, gönder, geri al...) önerilmez, açık komut istenir."""
        intent = self.parser.parse_command(fixed)
        if intent.type == IntentType.UNKNOWN:
            return AssistantResponse("Komutu anlayamadım. Komut listesi için 'Jarvis, yardım' deyin.",
                                     IntentType.UNKNOWN, ok=False)
        if intent.type in EXPLICIT_ONLY_INTENTS:
            example = EXPLICIT_ONLY_INTENTS[intent.type]
            return AssistantResponse(
                f"Bu önemli bir işlem; yalnızca açık komutla yapılır. Lütfen «Jarvis, {example}» deyin.",
                IntentType.UNKNOWN, ok=False)
        with self._lock:
            self._new_pending(intent, fixed)
        heard_text = f"{heard_wake} {heard}".strip() if heard_wake and not heard.startswith(heard_wake) else heard
        return AssistantResponse(
            f"Ses tanıma «{heard_text}» olarak duydu. «Jarvis, {fixed}» mı demek istediniz? "
            "Onaylamak için «Jarvis, evet», vazgeçmek için «Jarvis, hayır» deyin.", IntentType.UNKNOWN, ok=True)

    # ---------------------------------------------------------------- yerel LLM (Faz 5)
    def _route_unknown(self, intent: Intent) -> AssistantResponse:
        """Kurallı ayrıştırıcının anlamadığı, Jarvis'e AÇIKÇA hitap edilmiş komutu LLM'ye sorar.

        LLM yalnızca öneri üretir: okuma komutları çalışır, kayıt değiştirenler onay bekler,
        önemli işlemler hiç çalışmaz. LLM yoksa/hata verirse eski yanıt verilir.
        """
        unknown = AssistantResponse("Komutu anlayamadım. Komut listesi için 'Jarvis, yardım' deyin.",
                                    IntentType.UNKNOWN, ok=False)
        router = self.llm.router
        command = str(intent.get("command", "")).strip()
        if not command or not self.parser.is_addressed(intent.raw_text or ""):
            return unknown
        opened = self.flow.open_bare_record(command)  # "yeni bir görev ekleyelim" → içerik sorulur (Faz 5c.2)
        if opened is not None:
            return opened
        if router is None:  # LLM kapalı: önemli işlem, olumsuz istek, çoklu işlem ve selamlaşma yine tanınır
            self.trace.note(asama="llm_kapalı", yonlendirici="kapalı")
            hint = explicit_only_hint(command)
            if hint is not None:
                return AssistantResponse(hint.message, IntentType.UNKNOWN, ok=False)
            guard = pre_llm_guard(command)
            if guard is None:
                return unknown
            if guard.category == "incomplete":
                opened = self.flow.open_attendance_rest(command)
                if opened is not None:
                    return opened
            return AssistantResponse(guard.message, IntentType.UNKNOWN, ok=guard.kind == "no_action")
        with self._lock:
            meeting = self.manager.meeting if self.manager.has_active_meeting else None
            participants = [p.name for p in meeting.participants] if meeting else []
            meeting_date = meeting.started_at.date() if meeting else None
            current = meeting.current_item.title if meeting and meeting.current_item else ""
        decision = router.route(command, participants, meeting_date, current)
        log.info("LLM yönlendirme: %s -> %s (%s/%s, %.2f sn) %s", command, decision.llm_intent or "-", decision.kind,
                 decision.category or "-", decision.latency_s, decision.rejected_reason)
        self.trace.note(asama="llm", yonlendirici=decision.kind, llm_kategori=decision.category,
                        llm_niyet=decision.llm_intent, llm_sn=round(decision.latency_s, 2),
                        llm_ret=decision.rejected_reason[:80] if decision.rejected_reason else None)
        if decision.kind == "execute" and decision.intent is not None:
            result = self.handle_intent(decision.intent)
            return AssistantResponse(f"{decision.message} {result.message}".strip(), result.intent, ok=result.ok)
        if decision.kind == "confirm" and decision.intent is not None:
            with self._lock:
                self._new_pending(decision.intent, decision.canonical)
            return AssistantResponse(decision.message, IntentType.UNKNOWN, ok=True)
        if decision.kind == "no_action":  # olumsuz istek, soru, varsayım, sohbet: bilinçli olarak işlem yok
            return AssistantResponse(decision.message, IntentType.UNKNOWN, ok=True)
        if decision.kind == "clarify":
            with self._lock:
                opened = self._open_from_clarify(decision, command)
            if opened is not None:
                return opened
        if decision.kind in ("blocked", "clarify"):
            return AssistantResponse(decision.message, IntentType.UNKNOWN, ok=False)
        return unknown

    def _open_from_clarify(self, decision, command: str) -> AssistantResponse | None:
        """Yönlendiricinin açıklama isteğini çok turlu soruya çevirir (yalnızca deterministik durumlarda)."""
        if decision.category == "incomplete":
            return self.flow.open_attendance_rest(command)
        candidates = decision.details.get("candidates")
        if candidates and decision.details.get("intent") == IntentType.SET_SPEAKER.value:
            return self.flow.open_choose(list(candidates), "speaker")
        return None

    def _participant_change(self, intent: Intent, op: str) -> OperationResult:
        """Katılımcı düzeltmesi (Faz 5c.2). Önce yalnızca kontrol edilir ve onay istenir; RAM ve SQLite onaya kadar
        değişmez. Onaylanan öneri yeniden doğrulanarak uygulanır."""
        name, new_name = str(intent.get("name", "")), str(intent.get("new_name", ""))
        if intent.get("confirmed"):
            return self.manager.change_participant(op, name, new_name)
        plan = self.manager.plan_participant_change(op, name, new_name)
        if plan.ok:
            self._propose_participant_plan(intent.type, op, plan)
            return OperationResult(True, f"{plan.message} Onaylamak için «evet», vazgeçmek için «hayır» deyin.",
                                   plan.data)
        return plan

    def _propose_participant_plan(self, intent_type: IntentType, op: str, plan: OperationResult) -> None:
        params = {"name": plan.data["name"], "new_name": plan.data.get("new_name", ""), "confirmed": True}
        canonical = f"{op}: {params['name']}" + (f" → {params['new_name']}" if params["new_name"] else "")
        self._new_pending(Intent(intent_type, params, canonical), canonical)

    def propose_participant(self, op: str, name: str, new_name: str = "") -> AssistantResponse:
        """Diyalogda belirsiz ad seçildikten sonra aynı işlemin onay önerisini hazırlar (kayıt yapmaz)."""
        intent_type = {"absent": IntentType.MARK_ABSENT, "remove": IntentType.REMOVE_PARTICIPANT,
                       "rename": IntentType.RENAME_PARTICIPANT}[op]
        result = self._participant_change(Intent(intent_type, {"name": name, "new_name": new_name}), op)
        follow = self._after_handler(Intent(intent_type, {}), result)
        return follow or AssistantResponse(result.message, IntentType.UNKNOWN, ok=result.ok)

    def _mark_present(self, intent: Intent) -> OperationResult:
        """Yoklama komutu. Listede olmayan ve tek kişi mi birden çok kişi mi olduğu belli olmayan adlar
        varsayımla eklenmez; "Jarvis, evet" onayı beklenir (Faz 5c.1)."""
        confirmed = bool(intent.get("confirmed_new"))
        result = self.manager.mark_present(intent.get("names", []), ask_uncertain=not confirmed,
                                           implicit=bool(intent.get("implicit")))
        names = result.data.get("confirm_names") or []
        if names:  # kısmi kayıt yok: onaylanırsa listedeki herkes birlikte işlenir
            follow_up = Intent(IntentType.MARK_PRESENT_MANY, {"names": list(names), "confirmed_new": True},
                               intent.raw_text)
            self._new_pending(follow_up, f"katılımcılar: {', '.join(names)}")
        return result

    def _confirm_pending(self) -> OperationResult:
        pending, self.pending = self.pending, None
        if pending is None:
            question = self.dialog.active_question()
            if question is not None:
                return OperationResult(False, f"Onaylanacak bir işlem yok; önce soruyu yanıtlayın: {question.question}")
            return OperationResult(False, "Onay bekleyen bir komut yok.")
        if pending.expired(self.dialog.clock()):
            return OperationResult(False, "Önerinin onay süresi doldu; hiçbir işlem yapılmadı. Komutu tekrar söyleyin.")
        log.info("LLM önerisi kullanıcı tarafından onaylandı: %s", pending.canonical)
        response = self.handle_intent(pending.intent)
        index = pending.intent.params.get("transcript_index")
        if response.ok and isinstance(index, int) and self.manager.has_active_meeting:
            # hitapsız duyulan komut onayla işlendi: kayıt silinmez, "komut olarak işlendi" diye işaretlenir
            self.manager.mark_transcript(index, TranscriptKind.EXECUTED, str(pending.intent.params.get(
                "transcript_text", "")))
        return OperationResult(response.ok, response.message)

    def _reject_pending(self) -> OperationResult:
        pending, self.pending = self.pending, None
        question, self.dialog.question = self.dialog.question, None
        if pending is None and question is not None:
            return OperationResult(True, "Tamam, vazgeçildi; hiçbir işlem yapılmadı.")
        if pending is None:
            return OperationResult(False, "Onay bekleyen bir komut yok.")
        return OperationResult(True, "Tamam, öneri uygulanmadı.")

    def _llm_busy(self) -> bool:
        return self._llm_job is not None and self._llm_job.is_alive()

    def _run_llm_job(self, name: str, work: Callable[[], str]) -> None:
        """Uzun LLM işini arka planda çalıştırır; bitince sonucu seslendirir."""
        def runner() -> None:
            try:
                message = work()
            except Exception:
                log.exception("%s sırasında beklenmeyen hata", name)
                message = f"{name} tamamlanamadı; ayrıntı logs/jarvis.log dosyasında. Toplantı kaydı etkilenmedi."
            if message:
                self.speech.say(message)
        self._llm_job = threading.Thread(target=runner, daemon=True, name=f"llm-{name}")
        self._llm_job.start()

    def _start_analysis(self) -> OperationResult:
        with self._lock:
            meeting = self.manager._require_meeting()
            snapshot = Meeting.from_dict(meeting.to_dict())
        if self._llm_busy():
            return OperationResult(False, "Önceki yapay zekâ işlemi sürüyor; bitince bildireceğim.")
        analyzer = self.llm.analyzer

        def work() -> str:
            result = analyzer.analyze(snapshot)
            with self._lock:
                live = self.manager.meeting
                if live is None or live.id != snapshot.id:
                    return "Analiz tamamlandı ancak toplantı değiştiği için sonuç kaydedilmedi."
                drafts = candidates_to_drafts(result.candidates, live, 1)
                summary = {"method": result.method, "overall": result.overall_summary,
                           "items": result.item_summaries, "warnings": result.warnings,
                           "created_at": self.manager.clock().replace(microsecond=0).isoformat(),
                           "stats": dict(result.stats.__dict__)}
                stored = self.manager.store_analysis(live, drafts, summary)
            warning = f" {' '.join(result.warnings)}" if result.warnings else ""
            return stored.message + warning

        if not self.background_llm:
            return OperationResult(True, work())
        self._run_llm_job("Toplantı analizi", work)
        model = f"yerel model {self.llm.model}" if analyzer.uses_llm else "kural tabanlı yöntem"
        return OperationResult(True, f"Toplantı analizi başladı ({model}). Bu biraz sürebilir; toplantıya devam "
                                     "edebilirsiniz, bittiğinde bildireceğim.")

    def _ask_history(self, question: str) -> OperationResult:
        question = (question or "").strip()
        if not question:
            return OperationResult(False, "Soruyu anlayamadım. Örnek: 'Jarvis, soru: geçen toplantıda bütçe için "
                                          "ne karar verdik?'")
        repo, demo = self.manager.repo, self.manager.demo
        with self._lock:  # iş parçacığı canlı nesneye dokunmasın diye anlık kopya
            current = Meeting.from_dict(self.manager.meeting.to_dict()) if self.manager.meeting else None

        def work() -> str:
            headers = repo.list_meetings(limit=12, include_demo=demo)
            meetings = [m for m in (repo.get(h["id"]) for h in headers) if m is not None]
            if current is not None and all(m.id != current.id for m in meetings):
                meetings.insert(0, current)
            answer = answer_question(question, meetings, self.llm.client, self.llm.model, self.llm.qa_timeout_s)
            return answer.message

        if not self.background_llm or self.llm.client is None:
            return OperationResult(True, work())
        if self._llm_busy():
            return OperationResult(False, "Önceki yapay zekâ işlemi sürüyor; biraz sonra tekrar sorun.")
        self._run_llm_job("Soru-cevap", work)
        return OperationResult(True, "Kayıtlara bakıyorum; cevabı birazdan söyleyeceğim.")

    def _end_meeting(self) -> OperationResult:
        """Toplantıyı kapatır (önce kayıt), sonra PDF üretir ve ayara göre e-posta gönderir.

        PDF veya e-posta hatası toplantı kaydını etkilemez; toplantı zaten kapatılıp kaydedilmiştir.
        """
        result = self.manager.end_meeting()
        if not result.ok or self.minutes is None or self.manager.meeting is None:
            return result
        final = self.minutes.finalize(self.manager.meeting)
        return OperationResult(result.ok, f"{result.message} {final.message}", {**result.data, "minutes": final.data})

    def _next_meeting(self, intent: Intent) -> OperationResult:
        """Sonraki toplantı tarihi (+ saat varsa takvim daveti). Saat yoksa davet oluşturulmaz, uydurulmaz."""
        result = self.manager.set_next_meeting(intent.get("date_text", ""))
        if not result.ok or self.minutes is None or self.manager.meeting is None:
            return result
        if result.data.get("time"):
            invite = self.minutes.create_invite(self.manager.meeting)
            return OperationResult(invite.ok, f"{result.message} {invite.message}", {**result.data, **invite.data})
        note = " Saat belirtilmediği için takvim daveti oluşturulmadı."
        if result.data.get("stale_invite"):
            note += " Önceki takvim daveti artık geçerli değil; e-postaya eklenmeyecek."
        return OperationResult(True, result.message + note, result.data)

    def _invite(self, attach: bool) -> OperationResult:
        if self.minutes is None:
            return OperationResult(False, "Tutanak servisi yapılandırılmamış.")
        meeting = self._minutes_target()
        if meeting is None:
            return OperationResult(False, "Davet oluşturulacak toplantı bulunamadı.")
        return self.minutes.attach_invite(meeting) if attach else self.minutes.create_invite(meeting)

    def _export_tasks(self, scope: str) -> OperationResult:
        if self.minutes is None:
            return OperationResult(False, "Tutanak servisi yapılandırılmamış.")
        return self.minutes.export_tasks(self._minutes_target(), scope)

    def _minutes_target(self) -> Meeting | None:
        """Tutanak komutlarının hedefi: açık/son kapatılan toplantı, yoksa veritabanındaki son toplantı."""
        if self.manager.meeting is not None:
            return self.manager.meeting
        return self.manager.repo.get_last_ended(include_demo=self.manager.demo)

    def _send_minutes(self, force: bool) -> OperationResult:
        if self.minutes is None:
            return OperationResult(False, "Tutanak servisi yapılandırılmamış.")
        meeting = self._minutes_target()
        if meeting is None:
            return OperationResult(False, "Gönderilecek tutanak bulunamadı.")
        return self.minutes.send_minutes(meeting, force=force)

    def _create_minutes(self) -> OperationResult:
        if self.minutes is None:
            return OperationResult(False, "Tutanak servisi yapılandırılmamış.")
        meeting = self._minutes_target()
        if meeting is None:
            return OperationResult(False, "Tutanağı oluşturulacak toplantı bulunamadı.")
        return self.minutes.create_pdf(meeting)

    def _exit_message(self) -> str:
        if self.manager.has_active_meeting:
            return ("Program kapatılıyor. Açık toplantı kaydedildi; programı yeniden "
                    "başlattığınızda kaldığınız yerden devam edebilirsiniz.")
        return "Görüşmek üzere."

    # ---------------------------------------------------------------- döngü
    def respond(self, response: AssistantResponse) -> None:
        if not response.message:
            return
        note = getattr(self.speech, "note", None)
        if not response.speak and note is not None:
            note(response.message)
            return
        self.speech.say(response.message)

    def escape_pressed(self) -> None:
        """Esc tuşu: yalnızca konuşuyorsa sesi keser. Komut, onay, soru veya toplantı durumu değişmez."""
        if getattr(self.speech, "is_speaking", False) and self.stop_speaking():
            note = getattr(self.speech, "note", None)
            if note is not None:
                note("(Esc: sesli yanıt durduruldu.)")

    def stop_speaking(self) -> bool:
        """Çalan ve bekleyen seslendirmeyi keser (Esc tuşu, "Jarvis, dur"). Uygulamayı kapatmaz; veriye dokunmaz."""
        cancel = getattr(self.speech, "cancel_all", None)
        if cancel is None:
            return False
        try:
            cancel()
        except Exception:
            log.exception("Seslendirme durdurulamadı")
            return False
        return True

    def startup(self) -> None:
        """Açılışta yarım kalan toplantıyı kurtarır ve kullanıcıyı bilgilendirir."""
        with self._lock:
            recovered = self.manager.recover_active_meeting()
        if recovered:
            self.speech.say(recovered.message)

    def run(self, source: InputSource, monitor_interval: float | None = TIMEBOX_CHECK_SECONDS) -> None:
        """Ana döngü. Ctrl+C ile kapatılırsa veriler zaten kaydedilmiş durumdadır."""
        self.startup()
        monitor = TimeboxMonitor(self, monitor_interval) if monitor_interval else None
        if monitor:
            monitor.start()
        interrupted = False
        try:
            while True:
                line = source.listen()
                if line is None:
                    break
                response = self.handle_text(line)
                self.respond(response)
                self._log_latency(line)
                if response.stop:
                    break
        except KeyboardInterrupt:
            interrupted = True
        finally:
            if monitor:
                monitor.stop()
            source.close()  # sesli girişte yarım konuşma ve bekleyen bölütler burada tanınır
            self._drain_source(source)
            if interrupted:
                self.on_interrupt()

    def _drain_source(self, source: InputSource) -> None:
        """Kapanışta tanınan ama işlenmemiş konuşmaları transkripte yazar.

        Bu aşamada gelen komutlar güvenlik için çalıştırılmaz (ör. kapanırken yanlışlıkla
        "toplantıyı bitir" algılanması); yalnızca kayda geçirilir.
        """
        drain = getattr(source, "drain", None)
        if drain is None:
            return
        for utterance in drain():
            with self._lock:
                if self.parser.parse(utterance, require_wake=self.require_wake) is not None:
                    log.warning("Kapanış sırasında gelen komut uygulanmadı: %s", utterance)
                    continue
                self._handle_speech(utterance, getattr(utterance, "captured_at", None), _utterance_seconds(utterance),
                                    classify=True, propose=False)

    @staticmethod
    def _log_latency(line: object) -> None:
        """Sesli girişte konuşmanın bitişinden yanıtın verilmesine kadar geçen süreyi kaydeder (T-01)."""
        ended_at = getattr(line, "voice_ended_at", None) or getattr(line, "ended_at", None)
        if ended_at is None:
            return
        total = (datetime.now() - ended_at).total_seconds()
        log.info("Gecikme: STT %.2f sn, konuşma sonundan yanıta toplam %.2f sn",
                 getattr(line, "stt_latency", 0.0) or 0.0, total)

    def on_interrupt(self) -> None:
        """Ctrl+C: toplantı kaydedilir ve ara tutanak PDF'i üretilir (T-10).

        Toplantı kapatılmaz; program yeniden açıldığında kaldığı yerden devam eder.
        """
        with self._lock:
            try:
                self.manager.save()
            except StorageError:
                log.exception("Kapanışta kayıt hatası")
            interim = None
            if self.manager.has_active_meeting and self.minutes is not None:
                assert self.manager.meeting is not None
                interim = self.minutes.create_pdf(self.manager.meeting, interim=True)
        if self.manager.has_active_meeting:
            message = "Program durduruldu. Toplantı kaydedildi; yeniden başlattığınızda kurtarılacak."
            if interim is not None:
                message += f" {interim.message}"
            self.speech.say(message)
        else:
            self.speech.say("Program durduruldu.")


def _utterance_seconds(text: object) -> float | None:
    """Sesli girişte konuşma bölümünün süresi (yakalama başı → kullanıcının sustuğu an). Klavyede None."""
    started = getattr(text, "captured_at", None)
    ended = getattr(text, "voice_ended_at", None) or getattr(text, "ended_at", None)
    if started is None or ended is None:
        return None
    seconds = (ended - started).total_seconds()
    return seconds if 0 <= seconds <= 600 else None


def _parser_reason(intent: Intent) -> str | None:
    if intent.type != IntentType.UNKNOWN:
        return None
    for key in ("stt_suggestion", "incomplete_intent", "clarify", "reason"):
        if intent.get(key):
            return key
    return "kural_yok"
