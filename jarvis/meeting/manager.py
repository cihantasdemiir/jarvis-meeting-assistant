"""MeetingManager: toplantı iş kurallarının tek merkezi.

Ses katmanından bağımsızdır; yalnızca düz Python çağrılarıyla çalışır ve her
durum değişikliğinden sonra toplantıyı SQLite'a kaydeder.

Her işlem bir `OperationResult` döndürür: `ok`, kullanıcıya okunacak Türkçe
`message` ve gerekirse yapılandırılmış `data`.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from config.settings import Settings

from ..utils.dates import find_date, find_time, format_tr_date, remove_span
from ..utils.turkish_text import (
    ascii_fold,
    best_name_match,
    expand_name_chunks,
    is_none_value,
    locative_suffix,
    normalize_spaces,
    plausible_person_name,
    tr_title,
)
from .action_extractor import extract_action
from .loaders import load_agenda, load_participants
from .models import (
    ActionItem,
    ActionStatus,
    AgendaItem,
    AiDraft,
    DraftKind,
    DraftStatus,
    AgendaStatus,
    Decision,
    Meeting,
    MeetingStatus,
    Participant,
    SpeakerTurn,
    Vote,
    now,
)
from . import participant_ops
from .speaker_tracker import SpeakerStat, SpeakerTracker
from .storage import MeetingRepository, StorageError
from .summarizer import MeetingSummarizer, describe_action
from .transcript_service import TranscriptService

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


@dataclass
class OperationResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class NoActiveMeeting(RuntimeError):
    pass


def _word_matches(query_word: str, text_word: str) -> bool:
    """Ekli Türkçe kelimeleri eşleştirir: "rapor" ~ "raporu", "hazırlama" ~ "hazırlasın"."""
    if query_word == text_word:
        return True
    common = 0
    for a, b in zip(query_word, text_word, strict=False):
        if a != b:
            break
        common += 1
    return common >= 4 and common >= len(query_word) - 3


class MeetingManager:
    def __init__(self, repository: MeetingRepository, settings: Settings, clock: Clock = now,
                 demo: bool = False):
        self.repo = repository
        self.settings = settings
        self.clock = clock
        self.demo = demo
        self.meeting: Meeting | None = None
        self.summarizer = MeetingSummarizer()

    # ================================================================ yardımcılar
    def _now(self) -> datetime:
        return self.clock().replace(microsecond=0)

    def save(self) -> None:
        if self.meeting is not None:
            self.repo.save(self.meeting)

    def _require_meeting(self) -> Meeting:
        if self.meeting is None or not self.meeting.is_active:
            raise NoActiveMeeting("Aktif toplantı yok. Önce 'toplantıyı başlat' deyin.")
        return self.meeting

    @property
    def has_active_meeting(self) -> bool:
        return self.meeting is not None and self.meeting.is_active

    def _tracker(self) -> SpeakerTracker:
        return SpeakerTracker(self._require_meeting())

    def _current_item_title(self) -> str:
        meeting = self._require_meeting()
        item = meeting.current_item
        return item.title if item else ""

    def _meeting_date(self):
        return self._require_meeting().started_at.date()

    # ================================================================ yaşam döngüsü
    def recover_active_meeting(self) -> OperationResult | None:
        """Program yeniden açıldığında yarım kalan toplantıyı geri yükler (G-03)."""
        active = self.repo.get_active(include_demo=self.demo)
        if active is None or (active.is_demo != self.demo):
            return None
        self.meeting = active
        item = active.current_item
        detail = f" Şu anki madde: {item.title}." if item else ""
        closed = self._close_turn_at_last_activity(active)
        if closed is not None:
            # Program kapalıyken geçen süre söz süresine eklenmez: eski tur son kayıt anında kapatılır, söz aynı
            # kişide yeni (devam) turuyla sürer; söz alma sayısı artmaz (Faz 5c.2 canlı düzeltme).
            active.speaker_turns.append(SpeakerTurn(speaker=closed.speaker, started_at=max(self._now(),
                                                                                          closed.ended_at),
                                                    agenda_item=closed.agenda_item, resumed=True))
            active.active_speaker = closed.speaker
            gap = (self._now() - closed.ended_at).total_seconds()
            if gap >= 60:
                detail += (f" Söz {locative_suffix(closed.speaker)}; program kapalıyken geçen yaklaşık "
                           f"{round(gap / 60)} dakika söz süresine sayılmadı. Söz başkasındaysa 'Jarvis, söz Ad'da' "
                           "deyin.")
            self.save()
        return OperationResult(
            True,
            f"Yarım kalan toplantı kurtarıldı: {active.title} "
            f"({active.started_at:%d.%m.%Y %H:%M} başladı).{detail}",
            {"meeting_id": active.id},
        )

    @staticmethod
    def _close_turn_at_last_activity(meeting: Meeting):
        tracker = SpeakerTracker(meeting)
        turn = tracker.active_turn
        if turn is None:
            return None
        stamps = [turn.started_at, *(t.timestamp for t in meeting.transcript),
                  *(d.created_at for d in meeting.decisions if d.created_at),
                  *(a.created_at for a in meeting.action_items if a.created_at)]
        return tracker.close_active(max(stamps))

    def start_meeting(self, title: str | None = None) -> OperationResult:
        if self.has_active_meeting:
            assert self.meeting is not None
            return OperationResult(False, f"Zaten açık bir toplantı var: {self.meeting.title}. "
                                          "Önce 'toplantıyı bitir' deyin.")
        participants = load_participants(self.settings.participants_file)
        agenda = load_agenda(self.settings.agenda_file, self.settings.default_item_minutes)
        previous = self.repo.get_last_ended(include_demo=self.demo)

        meeting = Meeting(
            title=normalize_spaces(title) if title else self.settings.meeting_title,
            started_at=self._now(),
            chair=self.settings.meeting_chair,
            location=self.settings.meeting_location,
            participants=participants.items,
            agenda=agenda.items,
            previous_meeting_id=previous.id if previous else None,
            is_demo=self.demo,
            load_warnings=participants.warnings + agenda.warnings,
        )
        self.meeting = meeting
        if meeting.agenda:
            self._activate_item(0)
        self.save()
        log.info("Toplantı başlatıldı: %s (%s)", meeting.title, meeting.id)

        parts = [f"Toplantı başlatıldı: {meeting.title}."]
        if meeting.agenda:
            total = sum(item.planned_minutes for item in meeting.agenda)
            parts.append(f"Gündemde {len(meeting.agenda)} madde var, planlanan süre {total} dakika.")
            parts.append(f"İlk gündem maddesi: {meeting.agenda[0].title}.")
        else:
            parts.append("Gündem boş. 'Gündeme ekle' komutuyla madde ekleyebilirsiniz.")
        if not meeting.participants:
            parts.append("Katılımcı listesi yüklenemedi; yoklamada söylenen isimler eklenecek.")
        carryover = self.repo.get_open_actions(exclude_meeting_id=meeting.id, include_demo=self.demo)
        if carryover:
            parts.append(f"Önceki toplantılardan {len(carryover)} açık görev devrediyor.")
        return OperationResult(True, " ".join(parts), {
            "meeting_id": meeting.id,
            "agenda_count": len(meeting.agenda),
            "carryover_count": len(carryover),
            "warnings": list(meeting.load_warnings),
        })

    def end_meeting(self) -> OperationResult:
        """Toplantıyı kapatır: açık madde ve konuşmacı süresi kapanır, kapanış özeti üretilir."""
        if self.meeting is None:
            return OperationResult(False, "Kapatılacak aktif toplantı yok.")
        if not self.meeting.is_active:
            return OperationResult(False, "Bu toplantı zaten kapatılmış.", {"meeting_id": self.meeting.id})
        meeting = self.meeting
        stamp = self._now()
        self._close_current_item(stamp, AgendaStatus.DONE)
        tracker = SpeakerTracker(meeting)
        tracker.close_active(stamp)
        carryover_open = self.repo.get_open_actions(exclude_meeting_id=meeting.id, include_demo=self.demo)
        stats = tracker.stats()
        silent = tracker.silent_participants()
        meeting.summary = self.summarizer.closing_summary(meeting, stats, silent, len(carryover_open))
        meeting.ended_at = stamp
        meeting.status = MeetingStatus.ENDED
        meeting.last_record = None
        self.save()
        duration_min = round((stamp - meeting.started_at).total_seconds() / 60)
        log.info("Toplantı kapatıldı: %s", meeting.id)
        return OperationResult(True, f"Toplantı sona erdi. Süre: {duration_min} dakika. {meeting.summary}",
                               {"meeting_id": meeting.id, "duration_minutes": duration_min,
                                "summary": meeting.summary})

    # ================================================================ yoklama
    def _participant_names(self) -> list[str]:
        meeting = self._require_meeting()
        return [p.name for p in meeting.participants]

    def resolve_participant(self, spoken: str):
        """Söylenen ismi katılımcı listesinde arar (belirsizlikte seçim yapmaz)."""
        return best_name_match(spoken, self._participant_names())

    # ---------------------------------------------------------------- rehber (Faz 6 ad düzeltmesi)
    def _directory_extras(self) -> list[Participant]:
        """Rehberde (participants.json) olup bu toplantının listesinde OLMAYAN kişiler. Toplantının listesi açılışta
        rehberden kopyalanır; rehber sonradan güncellendiyse veya liste o an okunamadıysa "Mehmet" gibi yalnız ad
        yeni kişi olarak kaydediliyordu. Listeden bilinçli olarak çıkarılan kişiler geri getirilmez."""
        meeting = self._require_meeting()
        try:
            directory = load_participants(self.settings.participants_file).items
        except Exception:  # rehber okunamazsa eski davranış: yalnızca toplantı listesi
            log.exception("Rehber okunamadı")
            return []
        taken = {ascii_fold(p.name) for p in [*meeting.participants, *meeting.removed_participants]}
        return [p for p in directory if ascii_fold(p.name) not in taken]

    def known_names(self) -> list[str]:
        """Eşleştirmede kullanılan tam adlar: toplantı listesi + rehberde olup listede olmayanlar."""
        return self._participant_names() + [p.name for p in self._directory_extras()]

    def resolve_known(self, spoken: str):
        """Toplantı listesi ve rehber birlikte; birden çok aday varsa SEÇİM YAPILMAZ (belirsiz döner)."""
        return best_name_match(spoken, self.known_names())

    def _expand_name_chunks(self, names: list[str]) -> list[str]:
        """Virgülsüz STT çıktısını ayırır: "Kenan Ayşe" -> ["Kenan", "Ayşe"].

        Yalnızca parça bir bütün olarak eşleşmiyorsa ve her kelime ayrı ayrı
        tek bir katılımcıyla eşleşiyorsa bölünür; aksi halde olduğu gibi kalır.
        """
        return expand_name_chunks(names, self.known_names())

    def mark_present(self, names: list[str], ask_uncertain: bool = False, implicit: bool = False) -> OperationResult:
        """Yoklama. `ask_uncertain=True` (sesli/klavye komutu, Faz 5c.1): listede olmayan ve tek kişi mi birden
        çok kişi mi olduğu bilinemeyen adlar ("Furkan Demir", "Ayşe Furkan") eklenmez; `data["uncertain"]` ile
        döner ve kullanıcıya sorulur. `implicit=True` ("X geldi" biçimi): listede olmayan küçük harfli tek
        kelime de sorulur ("rapor geldi" bir katılımcı değildir).

        `ask_uncertain=True` iken listede belirsiz veya sorulacak bir ad varsa HİÇ KİMSE işaretlenmez (kısmi kayıt
        yok); onaydan sonra liste bir bütün olarak işlenir. Doğrudan API (`ask_uncertain=False`) eskisi gibidir.
        """
        meeting = self._require_meeting()
        names = self._expand_name_chunks(names)
        extras = {p.name: p for p in self._directory_extras()}
        known = self._participant_names() + list(extras)
        to_mark: list[Participant] = []
        to_add: list[str] = []
        from_directory: list[Participant] = []
        already: list[str] = []
        uncertain: list[str] = []
        similar: dict[str, list[str]] = {}
        ambiguous: dict[str, list[str]] = {}
        accepted: list[str] = []  # onaydan sonra yeniden işlenecek tam liste
        for spoken in names:
            spoken = normalize_spaces(spoken)
            if not spoken:
                continue
            match = best_name_match(spoken, known)
            if match.is_ambiguous:
                ambiguous[spoken] = match.candidates
                continue
            accepted.append(spoken)
            if match.found and match.name in extras:
                # rehberdeki kişi: tam adı, e-postası ve rolüyle eklenir (yalnız ad kaydedilmez)
                if all(p.name != match.name for p in from_directory):
                    from_directory.append(extras[match.name])
                else:
                    already.append(match.name)
                continue
            lookalikes = _similar_names(spoken, known) if not match.found else []
            if not match.found and ask_uncertain and (
                    len(spoken.split()) > 1 or (implicit and not spoken[:1].isupper()) or lookalikes):
                uncertain.append(spoken)
                if lookalikes:
                    similar[spoken] = lookalikes
                continue
            if match.found:
                participant = meeting.find_participant(match.name)  # type: ignore[arg-type]
                assert participant is not None
                if participant.present or participant in to_mark:
                    already.append(participant.name)
                else:
                    to_mark.append(participant)
                continue
            # Listede yok: yeni katılımcı olarak eklenir (PDF Bölüm 4, adım 3)
            new_name = tr_title(spoken)
            if meeting.find_participant(new_name) is None and new_name not in to_add:
                to_add.append(new_name)
            else:
                already.append(new_name)

        hold = ask_uncertain and bool(uncertain or ambiguous)
        marked: list[str] = []
        added: list[str] = []
        if not hold:
            for participant in to_mark:
                participant.present = True
                marked.append(participant.name)
            for new_name in to_add:
                meeting.participants.append(Participant(name=new_name, present=True, added_during_meeting=True))
                added.append(new_name)
            for person in from_directory:
                meeting.participants.append(Participant(name=person.name, email=person.email, role=person.role,
                                                        present=True, added_during_meeting=True))
                marked.append(person.name)
            self.save()

        parts = []
        if hold:
            parts.append("Yoklamaya henüz kimse eklenmedi.")
        total_new = len(marked) + len(added)
        if total_new:
            parts.append(f"{total_new} katılımcının yoklaması alındı.")
            if marked and len(marked) <= 4:
                parts.append(f"Kaydedilen: {', '.join(marked)}.")  # tam adla doğrulama (ör. "Mehmet" → tam ad)
        if added:
            parts.append(f"Listede olmayan {', '.join(added)} yeni katılımcı olarak eklendi.")
            if ask_uncertain and any(len(n.split()) == 1 for n in added):
                single = next(n for n in added if len(n.split()) == 1)
                parts.append(f"Soyadı uydurulmadı. Eklemek için «Jarvis, {single} olarak kaydettiğin kişinin adı "
                             "aslında» deyip ardından ad ve soyadı söyleyin.")
        if from_directory and not hold:
            parts.append(f"Rehberden eklendi: {', '.join(p.name for p in from_directory)}.")
        if already:
            parts.append(f"Zaten kayıtlı: {', '.join(already)}.")
        for spoken, candidates in ambiguous.items():
            parts.append(f"'{spoken}' birden fazla kişiyle eşleşiyor: {', '.join(candidates)}. Tam adı söyleyin.")
        if ambiguous and hold:
            parts.append("Listeyi tam adlarla tekrar söyleyin.")
        for spoken, lookalikes in similar.items():
            parts.append(f"'{spoken}' listede yok ama benzer kayıtlı kişi var: {', '.join(lookalikes)}. Aynı kişiyse "
                         "tam adını söyleyin; ikinci bir kayıt açılmadı.")
        if uncertain:
            listed = ", ".join(f"'{name}'" for name in uncertain)
            several = any(len(n.split()) > 1 for n in uncertain)
            doubt = ("tek kişi mi, ayrı kişiler mi olduğundan emin değilim" if several
                     else "bir katılımcı adı olduğundan emin değilim")
            if ambiguous:
                parts.append(f"Ayrıca {listed} katılımcı listesinde yok; {doubt}.")
            else:
                parts.append(f"{listed} katılımcı listesinde yok; {doubt}. Listedeki herkesi ({', '.join(accepted)}) "
                             "yazıldığı gibi işaretlemek için «Jarvis, evet» deyin; ayrı kişilerse adları «ve» ile "
                             "ayırarak tekrar söyleyin.")
        if not parts:
            parts.append("Yoklamaya eklenecek isim anlaşılamadı.")
        ok = bool(marked or added or already) and not ambiguous and not uncertain
        return OperationResult(ok, " ".join(parts), {
            "marked": marked, "added": added, "already": already, "ambiguous": ambiguous, "uncertain": uncertain,
            # onaylanırsa yeniden işlenecek tam liste (belirsiz eşleşme varsa onay önerilmez)
            # benzer ad varsa yeni kayıt yalnızca uyarı gösterildikten sonra açık "evet" ile açılır
            "confirm_names": accepted if uncertain and not ambiguous else [],
            "similar": similar,
            # belirsizlik sorulduktan sonra yeniden işlenecek, ÇÖZÜMLENMİŞ sözlü adlar (virgülsüz liste ayrılmış)
            "accepted": accepted,
        })

    # ================================================================ katılımcı düzeltmeleri (Faz 5c.2)
    def plan_participant_change(self, op: str, spoken: str, new_name: str = "") -> OperationResult:
        """Yoklama düzeltmesi / listeden çıkarma / ad düzeltme için ön kontrol. HİÇBİR ŞEYİ DEĞİŞTİRMEZ."""
        meeting = self._require_meeting()
        p = participant_ops.plan(meeting, op, spoken, new_name)
        return OperationResult(p.ok, p.message, {"op": op, "name": p.name, "new_name": p.new_name,
                                                 "merge": p.merge, "candidates": p.candidates,
                                                 "query": (new_name if p.name else spoken) if p.candidates else ""})

    def change_participant(self, op: str, name: str, new_name: str = "") -> OperationResult:
        """Onaylanmış katılımcı düzeltmesini uygular. Plan yeniden doğrulanır (bu arada durum değişmiş olabilir).
        Kayıt başarısız olursa bellekteki değişiklik geri alınır: yarım değişiklik kalmaz."""
        meeting = self._require_meeting()
        p = participant_ops.plan(meeting, op, name, new_name)
        if not p.ok:
            return OperationResult(False, f"İşlem yapılmadı: {p.message}")
        if p.name != name or (op == "rename" and p.new_name != new_name):
            return OperationResult(False, "Onaylanan kişi artık aynı biçimde eşleşmiyor; işlem yapılmadı. "
                                          "Komutu yeniden söyleyin.")
        snapshot = meeting.to_dict()
        try:
            message, undo = participant_ops.apply(meeting, p, self._now())
            meeting.last_record = {"kind": "participant", "ref": undo}
            self.save()
        except StorageError as exc:
            self.meeting = Meeting.from_dict(snapshot)  # yarım değişiklik kalmaz: bellek eski haline döner
            log.exception("Katılımcı düzeltmesi kaydedilemedi; geri alındı")
            return OperationResult(False, f"Kayıt hatası ({exc}); katılımcı düzeltmesi uygulanmadı, liste eski "
                                          "halinde. Sorun giderilince komutu tekrar söyleyin.")
        log.info("Katılımcı düzeltmesi: %s %s %s", op, name, new_name)
        return OperationResult(True, message + " Yanlışsa «Jarvis, geri al» deyin.", {"op": op, "undo": undo})

    def participant_roster(self) -> OperationResult:
        meeting = self._require_meeting()
        return OperationResult(True, participant_ops.roster_text(meeting))

    def attendance_status(self) -> OperationResult:
        meeting = self._require_meeting()
        present = [p.name for p in meeting.present_participants]
        absent = [p.name for p in meeting.absent_participants]
        if not meeting.participants:
            return OperationResult(True, "Katılımcı listesi boş.", {"present": [], "absent": []})
        parts = [f"{len(present)} kişi katıldı" + (f": {', '.join(present)}." if present else ".")]
        if absent:
            parts.append(f"Katılmayan {len(absent)} kişi: {', '.join(absent)}.")
        return OperationResult(True, " ".join(parts), {"present": present, "absent": absent})

    # ================================================================ gündem
    def _activate_item(self, index: int) -> AgendaItem:
        meeting = self._require_meeting()
        item = meeting.agenda[index]
        item.status = AgendaStatus.ACTIVE
        item.started_at = self._now()
        item.ended_at = None
        meeting.current_item_index = index
        return item

    def _close_current_item(self, stamp: datetime, status: str) -> AgendaItem | None:
        meeting = self.meeting
        if meeting is None:
            return None
        item = meeting.current_item
        if item is None or item.status != AgendaStatus.ACTIVE:
            return None
        if item.started_at:
            item.elapsed_seconds += max(0.0, (stamp - item.started_at).total_seconds())
        item.ended_at = stamp
        item.status = status
        meeting.current_item_index = None
        return item

    def _describe_item(self, item: AgendaItem, number: int) -> str:
        presenter = f", sunan: {item.presenter}" if item.presenter else ""
        return f"{number}. madde: {item.title} ({item.planned_minutes} dakika{presenter})"

    def read_agenda(self) -> OperationResult:
        meeting = self._require_meeting()
        if not meeting.agenda:
            return OperationResult(True, "Gündem boş.", {"items": []})
        lines = []
        for number, item in enumerate(meeting.agenda, start=1):
            status = "" if item.status == AgendaStatus.WAITING else f" [{item.status}]"
            lines.append(self._describe_item(item, number) + status)
        return OperationResult(True, f"Gündemde {len(meeting.agenda)} madde var. " + ". ".join(lines) + ".",
                               {"items": [item.title for item in meeting.agenda]})

    def _next_waiting_index(self, after: int) -> int | None:
        meeting = self._require_meeting()
        for index in range(after + 1, len(meeting.agenda)):
            if meeting.agenda[index].status == AgendaStatus.WAITING:
                return index
        # Sona gelindiyse başa dönüp bekleyen madde ara (ör. ertelenmeden önce atlananlar)
        for index in range(0, min(after + 1, len(meeting.agenda))):
            if meeting.agenda[index].status == AgendaStatus.WAITING:
                return index
        return None

    def next_item(self) -> OperationResult:
        meeting = self._require_meeting()
        if not meeting.agenda:
            return OperationResult(False, "Gündem boş.")
        stamp = self._now()
        current_index = meeting.current_item_index if meeting.current_item_index is not None else -1
        closed = self._close_current_item(stamp, AgendaStatus.DONE)
        next_index = self._next_waiting_index(current_index)
        if next_index is None:
            self.save()
            postponed = [i.title for i in meeting.agenda if i.status == AgendaStatus.POSTPONED]
            extra = f" Ertelenen maddeler: {', '.join(postponed)}." if postponed else ""
            done = f"'{closed.title}' tamamlandı. " if closed else ""
            return OperationResult(True, f"{done}Gündemde görüşülecek başka madde yok.{extra}",
                                   {"finished": True})
        item = self._activate_item(next_index)
        self.save()
        return OperationResult(True, f"Sıradaki {self._describe_item(item, next_index + 1)}.",
                               {"index": next_index, "title": item.title})

    def previous_item(self) -> OperationResult:
        meeting = self._require_meeting()
        if not meeting.agenda:
            return OperationResult(False, "Gündem boş.")
        current_index = meeting.current_item_index
        if current_index is None:
            # Hiç madde açık değilse sonuncu tamamlanan maddeye dön
            done = [i for i, item in enumerate(meeting.agenda) if item.status == AgendaStatus.DONE]
            if not done:
                return OperationResult(False, "Dönülecek önceki madde yok.")
            target = done[-1]
        else:
            if current_index == 0:
                return OperationResult(False, "Zaten ilk maddedesiniz.")
            target = current_index - 1
            stamp = self._now()
            self._close_current_item(stamp, AgendaStatus.WAITING)
            meeting.agenda[current_index].ended_at = None
        item = self._activate_item(target)
        self.save()
        return OperationResult(True, f"Önceki maddeye dönüldü. {self._describe_item(item, target + 1)}.",
                               {"index": target, "title": item.title})

    def skip_item(self) -> OperationResult:
        meeting = self._require_meeting()
        item = meeting.current_item
        if item is None:
            return OperationResult(False, "Şu anda görüşülen bir madde yok.")
        current_index = meeting.current_item_index or 0
        self._close_current_item(self._now(), AgendaStatus.POSTPONED)
        next_index = self._next_waiting_index(current_index)
        message = f"'{item.title}' ertelendi."
        data: dict[str, Any] = {"postponed": item.title}
        if next_index is not None:
            nxt = self._activate_item(next_index)
            message += f" Sıradaki {self._describe_item(nxt, next_index + 1)}."
            data["next"] = nxt.title
        else:
            message += " Görüşülecek başka madde yok."
        self.save()
        return OperationResult(True, message, data)

    def add_agenda_item(self, title: str, minutes: int | None = None, presenter: str = "") -> OperationResult:
        meeting = self._require_meeting()
        title = normalize_spaces(title)
        if not title:
            return OperationResult(False, "Gündem maddesinin başlığı anlaşılamadı.")
        planned = minutes if minutes and minutes > 0 else self.settings.default_item_minutes
        meeting.agenda.append(AgendaItem(title=title, planned_minutes=planned, presenter=presenter))
        if meeting.current_item_index is None and len(meeting.agenda) == 1:
            self._activate_item(0)
        self.save()
        return OperationResult(True, f"Gündeme eklendi: {title} ({planned} dakika). "
                                     f"Gündemde artık {len(meeting.agenda)} madde var.",
                               {"title": title, "minutes": planned})

    # ================================================================ süre yönetimi (G-12)
    def item_elapsed_seconds(self, item: AgendaItem) -> float:
        elapsed = item.elapsed_seconds
        if item.status == AgendaStatus.ACTIVE and item.started_at:
            elapsed += max(0.0, (self._now() - item.started_at).total_seconds())
        return elapsed

    def check_timebox(self) -> OperationResult | None:
        """Aktif madde planlanan sürenin %80'ine ulaştıysa bir kez uyarı döndürür.

        Uyarı verildiği maddeye kaydedilir; madde yeniden açılsa veya program
        yeniden başlatılsa da tekrar edilmez (T-05).
        """
        if not self.has_active_meeting:
            return None
        assert self.meeting is not None
        item = self.meeting.current_item
        if item is None or item.warning_given or item.planned_minutes <= 0:
            return None
        planned = item.planned_minutes * 60
        elapsed = self.item_elapsed_seconds(item)
        if elapsed < planned * self.settings.timebox_warning_percent / 100:
            return None
        item.warning_given = True
        self.save()
        remaining = planned - elapsed
        if remaining > 0:
            minutes = max(1, math.ceil(remaining / 60))
            message = f"'{item.title}' maddesi için yaklaşık {minutes} dakika kaldı."
        else:
            message = f"'{item.title}' maddesinin planlanan süresi doldu."
        return OperationResult(True, message, {"title": item.title, "remaining_seconds": remaining})

    def time_status(self) -> OperationResult:
        meeting = self._require_meeting()
        total_planned = sum(i.planned_minutes for i in meeting.agenda)
        meeting_minutes = math.floor((self._now() - meeting.started_at).total_seconds() / 60)
        parts = []
        item = meeting.current_item
        data: dict[str, Any] = {"meeting_minutes": meeting_minutes}
        if item is not None:
            elapsed = self.item_elapsed_seconds(item)
            remaining = item.planned_minutes * 60 - elapsed
            data.update({"item": item.title, "item_elapsed_seconds": elapsed, "item_remaining_seconds": remaining})
            if remaining >= 0:
                parts.append(f"'{item.title}' maddesinde {math.floor(elapsed / 60)} dakika geçti, "
                             f"{math.ceil(remaining / 60)} dakika kaldı.")
            else:
                parts.append(f"'{item.title}' maddesi planlanan süreyi {math.ceil(-remaining / 60)} dakika aştı.")
        else:
            parts.append("Şu anda görüşülen bir madde yok.")
        parts.append(f"Toplantı {meeting_minutes} dakikadır sürüyor; "
                     f"gündemin planlanan toplamı {total_planned} dakika.")
        return OperationResult(True, " ".join(parts), data)

    # ================================================================ konuşmacı (G-07)
    def set_speaker(self, spoken: str) -> OperationResult:
        meeting = self._require_meeting()
        spoken = normalize_spaces(spoken)
        if not spoken:
            return OperationResult(False, "Konuşmacının adı anlaşılamadı.")
        match = self.resolve_participant(spoken)
        if match.is_ambiguous:
            return OperationResult(False, f"'{spoken}' birden fazla kişiyle eşleşiyor: "
                                          f"{', '.join(match.candidates)}. Tam adı söyleyin.",
                                   {"ambiguous": match.candidates})
        if match.found:
            name = match.name or spoken
        elif meeting.participants or self._directory_extras():
            # Yanlış kişiye konuşma atfetmemek için listede olmayan isim kabul edilmez.
            known = self.resolve_known(spoken)
            hint = known.name if known.found else tr_title(spoken)
            where = " Rehberde kayıtlı." if known.found else ""
            return OperationResult(False, f"'{spoken}' bu toplantının katılımcı listesinde yok.{where} "
                                          f"Önce 'Jarvis, {hint} katıldı' diyerek ekleyin.")
        else:
            name = tr_title(spoken)
            meeting.participants.append(Participant(name=name, present=True, added_during_meeting=True))
        participant = meeting.find_participant(name)
        if participant is not None and not participant.present:
            participant.present = True  # söz alan kişi toplantıdadır
        changed = SpeakerTracker(meeting).set_speaker(name, self._now(), self._current_item_title())
        self.save()
        message = f"Söz {locative_suffix(name)}." if changed else f"Söz zaten {locative_suffix(name)}."
        return OperationResult(True, message, {"speaker": name, "changed": changed})

    def release_speaker(self) -> OperationResult:
        meeting = self._require_meeting()
        closed = SpeakerTracker(meeting).close_active(self._now())
        self.save()
        if closed is None:
            return OperationResult(False, "Şu anda söz kimsede değil.")
        return OperationResult(True, f"{closed.speaker} sözü bıraktı. Söz süresi: "
                                     f"{round(closed.duration_seconds / 60, 1)} dakika.",
                               {"speaker": closed.speaker})

    def speaker_stats(self) -> OperationResult:
        meeting = self._require_meeting()
        tracker = SpeakerTracker(meeting)
        stats = tracker.stats(now=self._now())
        if not stats:
            return OperationResult(True, "Henüz kimseye söz verilmedi.", {"stats": []})
        active = f"Şu anda söz {locative_suffix(meeting.active_speaker)}. " if meeting.active_speaker else ""
        lines = [f"{s.name} {s.minutes_text}, {s.turns} kez, {s.segments} konuşma kaydı" for s in stats]
        return OperationResult(True, active + "Söz süreleri (söz verildiği andan itibaren; gerçek konuşma ölçümü "
                                              "değildir): " + "; ".join(lines) + ".",
                               {"stats": [(s.name, s.total_seconds, s.turns) for s in stats]})

    def speaker_time(self, name: str, others: bool = False) -> OperationResult:
        """Bir kişinin (veya `others=True` ile onun dışındakilerin) söz süresi. Yalnızca okur (Faz 5c.2)."""
        meeting = self._require_meeting()
        now = self._now()
        tracker = SpeakerTracker(meeting)
        stats = {s.name: s for s in tracker.stats(now=now)}
        if others:
            rest = [s for n, s in stats.items() if n != name]
            silent = [p.name for p in meeting.present_participants if p.name not in stats and p.name != name]
            parts = ["; ".join(f"{s.name} {s.minutes_text}, {s.turns} kez" for s in rest)] if rest else []
            if silent:
                parts.append(f"henüz söz almayanlar: {', '.join(silent)}")
            if not parts:
                return OperationResult(True, f"{name} dışında kimse söz almadı.", {"others": []})
            return OperationResult(True, f"{name} dışındakiler: " + ". ".join(parts) + ".",
                                   {"others": [(s.name, s.total_seconds) for s in rest], "silent": silent})
        stat = stats.get(name)
        if stat is None:
            return OperationResult(True, f"{name} henüz söz almadı.", {"speaker": name, "seconds": 0})
        active = tracker.active_turn
        if active is not None and active.speaker == name:
            current = SpeakerStat(name, max(0.0, (now - active.started_at).total_seconds()), 1).minutes_text
            message = (f"{name} şu anki sözünde {current} oldu; toplam söz süresi {stat.minutes_text}, "
                       f"{stat.turns} kez söz aldı, {stat.segments} konuşma kaydı var. (Söz süresi, gerçek "
                       "konuşma ölçümü değildir.)")
        else:
            message = (f"{name} toplam {stat.minutes_text} söz sahibi oldu, {stat.turns} kez söz aldı, "
                       f"{stat.segments} konuşma kaydı var. Şu anda söz onda değil.")
        return OperationResult(True, message, {"speaker": name, "seconds": stat.total_seconds})

    def silent_participants(self) -> OperationResult:
        meeting = self._require_meeting()
        silent = SpeakerTracker(meeting).silent_participants()
        if not meeting.present_participants:
            return OperationResult(True, "Yoklama alınmadığı için söz almayanlar hesaplanamıyor.", {"silent": []})
        if not silent:
            return OperationResult(True, "Katılan herkes söz aldı.", {"silent": []})
        return OperationResult(True, f"Henüz söz almayanlar: {', '.join(silent)}.", {"silent": silent})

    # ================================================================ transkript (G-09)
    def add_transcript(self, text: str, speaker: str | None = None, at: datetime | None = None,
                       kind: str = "", duration_s: float | None = None) -> OperationResult:
        """Komut olmayan konuşmayı aktif konuşmacı adına transkripte yazar.

        `at`: konuşmanın söylendiği an (sesli girişte tanıma gecikmesini düzeltmek için).
        `kind`: boş = konuşma; TranscriptKind.COMMAND_LIKE = hitapsız komut kalıbı (silinmez, konuşma sayılmaz).
        """
        meeting = self._require_meeting()
        stamp = at.replace(microsecond=0) if at is not None else self._now()
        entries = TranscriptService(meeting).add(text, stamp, speaker, kind=kind, duration_s=duration_s)
        if not entries:
            return OperationResult(False, "Boş metin transkripte eklenmedi.")
        self.save()
        return OperationResult(True, "", {"speaker": entries[0].speaker, "index": len(meeting.transcript) - 1,
                                          "text": entries[-1].text})

    def mark_transcript(self, index: int, kind: str, text: str) -> bool:
        """Hitapsız duyulup sonradan onayla işlenen komutun transkript kaydını işaretler (silmez)."""
        meeting = self._require_meeting()
        if not 0 <= index < len(meeting.transcript) or meeting.transcript[index].text != text:
            return False
        meeting.transcript[index].kind = kind
        self.save()
        return True

    # ================================================================ kayıtlar (G-08, G-14)
    def _remember(self, kind: str, ref: Any) -> None:
        self._require_meeting().last_record = {"kind": kind, "ref": ref}

    def add_decision(self, text: str) -> OperationResult:
        meeting = self._require_meeting()
        text = normalize_spaces(text).strip(" .")
        if not text:
            return OperationResult(False, "Karar metni anlaşılamadı.")
        meeting.decisions.append(Decision(text=text, agenda_item=self._current_item_title(), created_at=self._now()))
        self._remember("decision", len(meeting.decisions) - 1)
        self.save()
        return OperationResult(True, f"Karar kaydedildi: {text}.", {"count": len(meeting.decisions)})

    def add_action(self, text: str, owner_declared_none: bool = False,
                   due_declared_none: bool = False) -> OperationResult:
        """`*_declared_none`: kullanıcı diyalogda "sorumlu yok" / "tarih yok" dedi; alan bilerek boş bırakılır ve
        "anlaşılamadı, belirtin" yönlendirmesi yapılmaz (aksi halde sonraki yanıt önceki göreve uygulanabiliyordu)."""
        meeting = self._require_meeting()
        text = normalize_spaces(text)
        if not text:
            return OperationResult(False, "Görev metni anlaşılamadı.")
        draft = extract_action(text, self.known_names(), self._meeting_date())
        action = ActionItem(text=draft.description, owner=draft.owner, due_date=draft.due_date,
                            agenda_item=self._current_item_title(), meeting_id=meeting.id,
                            created_at=self._now())
        meeting.action_items.append(action)
        self._remember("action", action.id)
        self.save()

        parts = [f"Görev kaydedildi: {action.text}."]
        parts.append(f"Sorumlu: {action.owner}." if action.owner else "")
        if action.due_date:
            note = f" ({draft.date_note})" if draft.date_note else ""
            parts.append(f"Son tarih: {format_tr_date(action.due_date)}{note}.")
        if draft.owner_ambiguous:
            parts.append(f"Sorumlu belirsiz ({', '.join(draft.owner_candidates)}). "
                         "'Jarvis, sorumlu: tam ad' diyerek belirtin.")
        elif not action.owner and owner_declared_none:
            parts.append("Sorumlu: belirtilmedi.")
        elif not action.owner:
            parts.append("Sorumlu anlaşılamadı. 'Jarvis, sorumlu: isim' diyerek belirtin.")
        if action.due_date is None and due_declared_none:
            parts.append("Son tarih: belirtilmedi.")
        elif action.due_date is None:
            parts.append("Son tarih anlaşılamadı. 'Jarvis, son tarih: tarih' diyerek belirtin.")
        return OperationResult(True, " ".join(p for p in parts if p), {
            "action_id": action.id, "owner": action.owner, "due_date": action.due_date,
            "description": action.text, "missing": draft.missing, "owner_candidates": draft.owner_candidates,
        })

    def _last_action(self) -> ActionItem | None:
        meeting = self._require_meeting()
        record = meeting.last_record or {}
        if record.get("kind") == "action":
            for action in meeting.action_items:
                if action.id == record.get("ref"):
                    return action
        return meeting.action_items[-1] if meeting.action_items else None

    def set_last_action_owner(self, spoken: str) -> OperationResult:
        action = self._last_action()
        if action is None:
            return OperationResult(False, "Bu toplantıda henüz görev kaydedilmedi.")
        spoken = normalize_spaces(spoken).strip(" .")
        if not spoken:
            return OperationResult(False, "Sorumlu adı anlaşılamadı.")
        if is_none_value(spoken) or is_none_value(f"sorumlu {spoken}"):
            # "sorumlu yok" bir kişi adı değildir; görev değiştirilmez (veri bütünlüğü, Faz 5c.2 canlı test)
            return OperationResult(False, f"«{spoken}» bir kişi adı değil; '{action.text}' görevinin sorumlusu "
                                          "değiştirilmedi.")
        match = self.resolve_known(spoken)
        if match.is_ambiguous:
            return OperationResult(False, f"'{spoken}' birden fazla kişiyle eşleşiyor: "
                                          f"{', '.join(match.candidates)}. Tam adı söyleyin.")
        note = ""
        if match.found:
            action.owner = match.name or spoken
        elif not plausible_person_name(spoken):
            # "yok son tarih yok", "Mehmet son tarih cuma" gibi karışık yanıtlar ad olarak kaydedilmez
            return OperationResult(False, f"«{spoken}» bir kişi adı olarak anlaşılamadı; görev değiştirilmedi. "
                                          "Sorumluyu ve tarihi ayrı ayrı söyleyin: 'Jarvis, sorumlu: Ad', "
                                          "'Jarvis, son tarih: cuma'.")
        else:
            # Kullanıcı açıkça söylediği için liste dışı sorumlu kabul edilir, ancak bildirilir.
            action.owner = tr_title(spoken)
            note = " (katılımcı listesinde yok)"
        self.save()
        return OperationResult(True, f"'{action.text}' görevinin sorumlusu: {action.owner}{note}.",
                               {"action_id": action.id, "owner": action.owner})

    def set_last_action_due(self, date_text: str) -> OperationResult:
        action = self._last_action()
        if action is None:
            return OperationResult(False, "Bu toplantıda henüz görev kaydedilmedi.")
        match = find_date(date_text, self._meeting_date())
        if match is None:
            return OperationResult(False, f"'{date_text}' tarih olarak anlaşılamadı. Örnek: '21 ekim' veya 'haftaya'.")
        action.due_date = match.value
        self.save()
        return OperationResult(True, f"'{action.text}' görevinin son tarihi: {format_tr_date(match.value)}.",
                               {"action_id": action.id, "due_date": match.value})

    def all_open_actions(self) -> list[ActionItem]:
        """Önce önceki toplantılardan devredenler, sonra bu toplantının açık görevleri."""
        meeting = self._require_meeting()
        carried = self.repo.get_open_actions(exclude_meeting_id=meeting.id, include_demo=self.demo)
        current = [a for a in meeting.action_items if a.status == ActionStatus.OPEN]
        return carried + current

    def open_actions(self) -> OperationResult:
        actions = self.all_open_actions()
        if not actions:
            return OperationResult(True, "Açık görev yok.", {"actions": []})
        lines = [f"{i}) {describe_action(a)}" for i, a in enumerate(actions, start=1)]
        return OperationResult(True, f"{len(actions)} açık görev var: " + "; ".join(lines) + ". "
                                     "Kapatmak için 'görev tamamlandı: numara veya görev adı' deyin.",
                               {"actions": [a.id for a in actions]})

    def _find_open_actions(self, query: str) -> list[ActionItem]:
        actions = self.all_open_actions()
        query = normalize_spaces(query).strip(" .")
        if query.isdigit():
            index = int(query) - 1
            return [actions[index]] if 0 <= index < len(actions) else []
        query_words = [w for w in ascii_fold(query).replace("'", " ").split() if len(w) >= 3]
        if not query_words:
            return []
        hits = []
        for action in actions:
            haystack = ascii_fold(f"{action.text} {action.owner}").replace("'", " ").split()
            if all(any(_word_matches(q, w) for w in haystack) for q in query_words):
                hits.append(action)
        return hits

    def _set_action_status(self, query: str, status: str) -> OperationResult:
        meeting = self._require_meeting()
        verb = "tamamlandı" if status == ActionStatus.DONE else "iptal edildi"
        hits = self._find_open_actions(query)
        if not hits:
            return OperationResult(False, f"'{query}' ile eşleşen açık görev bulunamadı. "
                                          "'Jarvis, açık görevleri oku' ile listeyi dinleyebilirsiniz.")
        if len(hits) > 1:
            options = "; ".join(f"{describe_action(a, with_date=False)}" for a in hits[:5])
            return OperationResult(False, f"Birden fazla görev eşleşti: {options}. Daha belirgin söyleyin "
                                          "veya numara verin.", {"candidates": [a.id for a in hits]})
        action = hits[0]
        if action.meeting_id == meeting.id:
            action.status = status
            action.completed_at = self._now() if status == ActionStatus.DONE else None
        else:
            updated = self.repo.update_action_status(action.id, status)
            if updated is None:
                return OperationResult(False, "Görev veritabanında bulunamadı.")
            if action.id not in meeting.carryover_completed:
                meeting.carryover_completed.append(action.id)
        self.save()
        return OperationResult(True, f"Görev {verb}: {describe_action(action, with_date=False)}.",
                               {"action_id": action.id, "status": status})

    def complete_action(self, query: str) -> OperationResult:
        return self._set_action_status(query, ActionStatus.DONE)

    def cancel_action(self, query: str) -> OperationResult:
        return self._set_action_status(query, ActionStatus.CANCELLED)

    def add_note(self, text: str) -> OperationResult:
        meeting = self._require_meeting()
        text = normalize_spaces(text).strip()
        if not text:
            return OperationResult(False, "Not metni anlaşılamadı.")
        item = meeting.current_item
        if item is not None:
            item.notes.append(text)
            self._remember("note", {"item": meeting.current_item_index, "index": len(item.notes) - 1})
        else:
            meeting.notes.append(text)
            self._remember("note", {"item": None, "index": len(meeting.notes) - 1})
        self.save()
        where = f" ({item.title})" if item else ""
        return OperationResult(True, f"Not alındı{where}.", {"text": text})

    def add_parking(self, text: str) -> OperationResult:
        meeting = self._require_meeting()
        text = normalize_spaces(text).strip(" .")
        if not text:
            return OperationResult(False, "Otoparka alınacak konu anlaşılamadı.")
        meeting.parking_lot.append(text)
        self._remember("parking", len(meeting.parking_lot) - 1)
        self.save()
        return OperationResult(True, f"Otoparka alındı: {text}. Otoparkta {len(meeting.parking_lot)} konu var.",
                               {"count": len(meeting.parking_lot)})

    def add_vote(self, topic: str, yes: int | None, no: int | None, abstain: int | None = None) -> OperationResult:
        meeting = self._require_meeting()
        topic = normalize_spaces(topic or "").strip(" .") or self._current_item_title() or "Oylama"
        if yes is None or no is None:
            return OperationResult(False, "Lehte ve aleyhte oy sayıları anlaşılamadı. "
                                          "Örnek: 'oylama: bütçe artışı lehte 5 aleyhte 2 çekimser 1'.")
        vote = Vote(topic=topic, yes=yes, no=no, abstain=abstain or 0,
                    agenda_item=self._current_item_title(), created_at=self._now())
        meeting.votes.append(vote)
        self._remember("vote", len(meeting.votes) - 1)
        self.save()
        result_text = {"KABUL": "kabul edildi", "RED": "reddedildi", "EŞİTLİK": "eşitlikle sonuçlandı"}[vote.result]
        message = (f"Oylama kaydedildi: {topic}. Lehte {vote.yes}, aleyhte {vote.no}, çekimser {vote.abstain}. "
                   f"Sonuç: {result_text}.")
        present = len(meeting.present_participants)
        total = vote.yes + vote.no + vote.abstain
        if present and total > present:
            message += f" Uyarı: toplam oy ({total}) katılımcı sayısından ({present}) fazla."
        return OperationResult(True, message, {"result": vote.result, "total": total})

    def undo_last(self) -> OperationResult:
        """Son eklenen karar/görev/not/otopark/oylama kaydını siler (yanlış anlaşılan komutlar için)."""
        meeting = self._require_meeting()
        record = meeting.last_record
        if not record:
            return OperationResult(False, "Geri alınacak kayıt yok.")
        kind, ref = record["kind"], record["ref"]
        removed = ""
        if kind == "decision" and 0 <= ref < len(meeting.decisions):
            removed = "Karar: " + meeting.decisions.pop(ref).text
        elif kind == "action":
            for index, action in enumerate(meeting.action_items):
                if action.id == ref:
                    removed = "Görev: " + meeting.action_items.pop(index).text
                    break
        elif kind == "parking" and 0 <= ref < len(meeting.parking_lot):
            removed = "Otopark: " + meeting.parking_lot.pop(ref)
        elif kind == "vote" and 0 <= ref < len(meeting.votes):
            removed = "Oylama: " + meeting.votes.pop(ref).topic
        elif kind == "note":
            notes = meeting.notes if ref["item"] is None else meeting.agenda[ref["item"]].notes
            if 0 <= ref["index"] < len(notes):
                removed = "Not: " + notes.pop(ref["index"])
        elif kind == "participant":
            text = participant_ops.undo(meeting, ref)
            removed = f"Katılımcı düzeltmesi: {text}" if text else ""
        draft_number = record.get("draft")
        if removed and draft_number:
            for draft in meeting.ai_drafts:
                if draft.number == draft_number:
                    draft.status, draft.record_ref = DraftStatus.DRAFT, ""
                    removed += f" (taslak {draft_number} yeniden onay bekliyor)"
        meeting.last_record = None
        self.save()
        if not removed:
            return OperationResult(False, "Son kayıt bulunamadı; geri alma yapılmadı.")
        return OperationResult(True, f"Geri alındı. {removed}.", {"kind": kind})

    # ================================================================ geçmiş (G-06, G-13)
    def previous_meeting(self) -> Meeting | None:
        meeting = self._require_meeting()
        if meeting.previous_meeting_id:
            previous = self.repo.get(meeting.previous_meeting_id)
            if previous is not None:
                return previous
        return self.repo.get_last_ended(exclude_id=meeting.id, include_demo=self.demo)

    def summarize_previous(self) -> OperationResult:
        previous = self.previous_meeting()
        message = self.summarizer.previous_meeting_summary(previous)
        return OperationResult(previous is not None, message,
                               {"previous_meeting_id": previous.id if previous else None})

    def set_next_meeting(self, date_text: str) -> OperationResult:
        """Sonraki toplantı tarihi ve (söylendiyse) saati. Saat söylenmediyse uydurulmaz; belirsiz veya geçersiz
        saatte (ör. "saat 2": 02.00 mı 14.00 mı?) HİÇBİR ŞEY kaydedilmez ve netleştirme istenir."""
        meeting = self._require_meeting()
        time_match = find_time(date_text)
        rest = date_text
        if time_match is not None:
            if time_match.error:
                return OperationResult(False, f"{time_match.error}; hiçbir şey kaydedilmedi. Örnek: 'saat 14.00'.")
            if time_match.ambiguous:
                return OperationResult(False, f"'{time_match.text}' sabah mı öğleden sonra mı belli değil; hiçbir "
                                              "şey kaydedilmedi. Lütfen 'saat 14.00' veya 'sabah saat 9' gibi "
                                              "söyleyin.")
            rest = remove_span(date_text, time_match.start, time_match.end)
        match = find_date(rest, self._meeting_date())
        if match is None:
            return OperationResult(False, f"'{date_text}' tarih olarak anlaşılamadı; hiçbir şey kaydedilmedi.")
        if match.value <= self._meeting_date():
            return OperationResult(False, "Sonraki toplantı tarihi bugünden sonra olmalı.")
        previous = (meeting.next_meeting_date, meeting.next_meeting_time)
        meeting.next_meeting_date = match.value
        meeting.next_meeting_time = time_match.hhmm if time_match else ""
        stale = bool(meeting.next_meeting_invite) and previous != (meeting.next_meeting_date,
                                                                   meeting.next_meeting_time)
        if stale and not meeting.next_meeting_time:
            # eski davet artık geçerli değil: e-postaya eklenmez (dosya silinmez)
            meeting.next_meeting_invite["attach"] = False
        self.save()
        when = format_tr_date(match.value)
        if time_match:
            when += f" saat {time_match.hhmm.replace(':', '.')}"
        return OperationResult(True, f"Sonraki toplantı {when} olarak kaydedildi.",
                               {"date": match.value, "time": meeting.next_meeting_time,
                                "stale_invite": stale and not meeting.next_meeting_time})

    # ================================================================ yapay zekâ taslakları (Öncelik 4)
    def store_analysis(self, meeting: Meeting, drafts: list[AiDraft], summary: dict[str, Any]) -> OperationResult:
        """Analiz sonucunu kaydeder. Önceki onay bekleyen taslakların yerini alır; onaylanmış/reddedilmiş
        taslaklar ve zaten resmî kayıt olan metinler tekrar önerilmez."""
        known = {ascii_fold(d.text) for d in meeting.ai_drafts if d.status != DraftStatus.DRAFT}
        known |= {ascii_fold(d.text) for d in meeting.decisions}
        known |= {ascii_fold(a.text) for a in meeting.action_items}
        kept = [d for d in meeting.ai_drafts if d.status != DraftStatus.DRAFT]
        number = max((d.number for d in kept), default=0)
        fresh = []
        for draft in drafts:
            if ascii_fold(draft.text) in known:
                continue
            number += 1
            draft.number = number
            fresh.append(draft)
        meeting.ai_drafts = kept + fresh
        meeting.ai_summary = dict(summary)
        self.repo.save(meeting)
        counts: dict[str, int] = {}
        for draft in fresh:
            counts[draft.kind] = counts.get(draft.kind, 0) + 1
        detail = ", ".join(f"{n} {kind}" for kind, n in counts.items()) or "yeni aday yok"
        return OperationResult(True, f"Analiz tamamlandı: {detail}. Bunlar taslaktır; resmî kayıt için "
                                     "'Jarvis, taslakları oku' ve 'Jarvis, taslak N onayla' deyin.",
                               {"drafts": len(fresh)})

    def _pending_drafts(self) -> list[AiDraft]:
        return [d for d in self._require_meeting().ai_drafts if d.status == DraftStatus.DRAFT]

    def list_drafts(self) -> OperationResult:
        drafts = self._pending_drafts()
        if not drafts:
            return OperationResult(True, "Onay bekleyen taslak yok. Taslak çıkarmak için "
                                         "'Jarvis, toplantıyı analiz et' deyin.", {"count": 0})
        parts = [f"{len(drafts)} taslak onay bekliyor."]
        for draft in drafts[:10]:
            parts.append(describe_draft(draft))
        if len(drafts) > 10:
            parts.append(f"... ve {len(drafts) - 10} taslak daha.")
        return OperationResult(True, " ".join(parts), {"count": len(drafts)})

    def _find_draft(self, number: int | None) -> AiDraft | None:
        for draft in self._require_meeting().ai_drafts:
            if draft.number == number:
                return draft
        return None

    def approve_draft(self, number: int | None) -> OperationResult:
        """İnsan onayı: taslağı resmî karar/görev/not yapar."""
        meeting = self._require_meeting()
        draft = self._find_draft(number)
        if draft is None:
            return OperationResult(False, f"{number} numaralı taslak bulunamadı.")
        if draft.status != DraftStatus.DRAFT:
            return OperationResult(False, f"Taslak {draft.number} zaten {draft.status}.")
        agenda = draft.agenda_item or self._current_item_title()
        if draft.kind == DraftKind.DECISION:
            meeting.decisions.append(Decision(text=draft.text, agenda_item=agenda, created_at=self._now()))
            self._remember("decision", len(meeting.decisions) - 1)
            message = f"Karar kaydedildi: {draft.text}."
        elif draft.kind == DraftKind.ACTION:
            action = ActionItem(text=draft.text, owner=draft.owner, due_date=draft.due_date, agenda_item=agenda,
                                meeting_id=meeting.id, created_at=self._now())
            meeting.action_items.append(action)
            self._remember("action", action.id)
            draft.record_ref = action.id
            message = f"Görev kaydedildi: {action.text}."
            message += f" Sorumlu: {action.owner}." if action.owner else \
                " Sorumlu belirtilmedi; 'Jarvis, sorumlu: isim' ile ekleyin."
            message += f" Son tarih: {format_tr_date(action.due_date)}." if action.due_date else \
                " Son tarih belirtilmedi; 'Jarvis, son tarih: tarih' ile ekleyin."
        else:
            meeting.notes.append(f"{tr_title(draft.kind)}: {draft.text}")
            self._remember("note", {"item": None, "index": len(meeting.notes) - 1})
            message = f"{tr_title(draft.kind)} notlara eklendi: {draft.text}."
        assert meeting.last_record is not None
        meeting.last_record["draft"] = draft.number
        draft.status = DraftStatus.APPROVED
        self.save()
        return OperationResult(True, message, {"draft": draft.number, "kind": draft.kind})

    def reject_draft(self, number: int | None) -> OperationResult:
        draft = self._find_draft(number)
        if draft is None:
            return OperationResult(False, f"{number} numaralı taslak bulunamadı.")
        if draft.status != DraftStatus.DRAFT:
            return OperationResult(False, f"Taslak {draft.number} zaten {draft.status}.")
        draft.status = DraftStatus.REJECTED
        self.save()
        return OperationResult(True, f"Taslak {draft.number} reddedildi; tutanağa girmeyecek.",
                               {"draft": draft.number})

    def read_ai_summary(self) -> OperationResult:
        summary = self._require_meeting().ai_summary
        text = (summary or {}).get("overall") or ""
        if not text:
            return OperationResult(False, "Henüz özet taslağı yok. 'Jarvis, toplantıyı analiz et' deyin.")
        return OperationResult(True, f"Özet taslağı ({summary.get('method', '?')}, onaylanmamış): {text}")


def describe_draft(draft: AiDraft) -> str:
    parts = [f"Taslak {draft.number} ({draft.kind}): {draft.text}."]
    if draft.kind == DraftKind.ACTION:
        parts.append(f"Sorumlu: {draft.owner or 'belirtilmedi'}.")
        parts.append(f"Son tarih: {format_tr_date(draft.due_date) if draft.due_date else 'belirtilmedi'}.")
    if draft.evidence:
        first = draft.evidence[0]
        stamp = f"{first.timestamp:%H:%M}" if first.timestamp else ""
        parts.append(f"Kaynak: {first.line} {stamp} {first.speaker}.".replace("  ", " "))
    return " ".join(parts)


def _similar_names(spoken: str, names: list[str], threshold: float = 0.75) -> list[str]:
    """Söylenen ad listede yok ama adı/soyadı çok benzeyen kayıtlı kişiler (ikinci kayıt oluşmasın diye sorulur).
    Örn. "Mehmet Kara" → Mehmet Öztürk (aynı ad), "Mehmed" → Mehmet Öztürk (yazım benzerliği)."""
    from difflib import SequenceMatcher  # noqa: PLC0415

    words = [ascii_fold(w) for w in spoken.split()]
    hits = []
    for name in names:
        name_words = ascii_fold(name).split()
        if words and name_words and words[0] == name_words[0] and len(words) > 1:
            hits.append(name)
            continue
        if any(SequenceMatcher(None, a, b).ratio() >= threshold for a in words for b in name_words if len(a) > 3):
            hits.append(name)
    return hits
