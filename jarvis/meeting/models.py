"""Toplantı veri modeli.

PDF'teki veri modeli (Bölüm 5) korunmuştur. Tüm tarih/saatler ISO 8601
biçiminde ("2026-10-14T10:30:00") yerel saat olarak saklanır.
Her sınıf `to_dict()` / `from_dict()` ile JSON'a çevrilebilir.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


class AgendaStatus:
    WAITING = "bekliyor"
    ACTIVE = "görüşülüyor"
    DONE = "tamamlandı"
    POSTPONED = "ertelendi"


class ActionStatus:
    OPEN = "açık"
    DONE = "tamamlandı"
    CANCELLED = "iptal"


class MeetingStatus:
    ACTIVE = "aktif"
    ENDED = "bitti"


class VoteResult:
    ACCEPTED = "KABUL"
    REJECTED = "RED"
    TIE = "EŞİTLİK"


def now() -> datetime:
    """Saniye hassasiyetinde yerel saat."""
    return datetime.now().replace(microsecond=0)


def dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def str_to_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def date_to_str(value: date | None) -> str | None:
    return value.isoformat() if value else None


def str_to_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@dataclass
class Participant:
    name: str
    email: str = ""
    role: str = ""
    present: bool = False
    # Liste dışından eklenen kişileri ayırt etmek için (PDF: "listede olmayanı yeni katılımcı olarak ekler").
    added_during_meeting: bool = False
    # Katılımcı düzeltmeleri (Faz 5c.2 katılımcı düzeltmesi). Yalnızca bu toplantının kaydında tutulur:
    former_name: str = ""  # ad düzeltildiyse ilk kaydedilen biçim
    removed_reason: str = ""  # toplantı listesinden çıkarıldıysa nedeni (kayıt silinmez)
    removed_at: datetime | None = None
    merged_into: str = ""  # yanlış kayıt başka bir katılımcıyla birleştirildiyse onun adı

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "email": self.email, "role": self.role, "present": self.present,
                                "added_during_meeting": self.added_during_meeting}
        # Yeni alanlar yalnızca doluysa yazılır: eski kayıtlarla birebir uyum
        if self.former_name:
            data["former_name"] = self.former_name
        if self.removed_reason:
            data["removed_reason"] = self.removed_reason
        if self.removed_at is not None:
            data["removed_at"] = dt_to_str(self.removed_at)
        if self.merged_into:
            data["merged_into"] = self.merged_into
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Participant":
        return cls(
            name=str(data["name"]).strip(),
            email=str(data.get("email") or "").strip(),
            role=str(data.get("role") or "").strip(),
            present=bool(data.get("present", False)),
            added_during_meeting=bool(data.get("added_during_meeting", False)),
            former_name=str(data.get("former_name") or ""),
            removed_reason=str(data.get("removed_reason") or ""),
            removed_at=str_to_dt(data.get("removed_at")),
            merged_into=str(data.get("merged_into") or ""),
        )


@dataclass
class AgendaItem:
    title: str
    planned_minutes: int = 10
    presenter: str = ""
    status: str = AgendaStatus.WAITING
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: list[str] = field(default_factory=list)
    # Süre uyarısı verildi mi? (aynı uyarının tekrarlanmaması için; G-12)
    warning_given: bool = False
    # Madde birden çok kez açılırsa geçen toplam süre (saniye)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "planned_minutes": self.planned_minutes,
            "presenter": self.presenter,
            "status": self.status,
            "started_at": dt_to_str(self.started_at),
            "ended_at": dt_to_str(self.ended_at),
            "notes": list(self.notes),
            "warning_given": self.warning_given,
            "elapsed_seconds": self.elapsed_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgendaItem":
        return cls(
            title=str(data["title"]).strip(),
            planned_minutes=int(data.get("planned_minutes") or 10),
            presenter=str(data.get("presenter") or "").strip(),
            status=data.get("status") or AgendaStatus.WAITING,
            started_at=str_to_dt(data.get("started_at")),
            ended_at=str_to_dt(data.get("ended_at")),
            notes=list(data.get("notes") or []),
            warning_given=bool(data.get("warning_given", False)),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
        )


@dataclass
class Decision:
    text: str
    agenda_item: str = ""
    created_at: datetime = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "agenda_item": self.agenda_item,
                "created_at": dt_to_str(self.created_at)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        return cls(text=data["text"], agenda_item=data.get("agenda_item") or "",
                   created_at=str_to_dt(data.get("created_at")) or now())


@dataclass
class ActionItem:
    text: str
    owner: str = ""
    due_date: date | None = None
    status: str = ActionStatus.OPEN
    agenda_item: str = ""
    meeting_id: str = ""
    id: str = field(default_factory=lambda: new_id("G-"))
    created_at: datetime = field(default_factory=now)
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "owner": self.owner,
            "due_date": date_to_str(self.due_date),
            "status": self.status,
            "agenda_item": self.agenda_item,
            "meeting_id": self.meeting_id,
            "created_at": dt_to_str(self.created_at),
            "completed_at": dt_to_str(self.completed_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionItem":
        return cls(
            id=data.get("id") or new_id("G-"),
            text=data["text"],
            owner=data.get("owner") or "",
            due_date=str_to_date(data.get("due_date")),
            status=data.get("status") or ActionStatus.OPEN,
            agenda_item=data.get("agenda_item") or "",
            meeting_id=data.get("meeting_id") or "",
            created_at=str_to_dt(data.get("created_at")) or now(),
            completed_at=str_to_dt(data.get("completed_at")),
        )


@dataclass
class Vote:
    topic: str
    yes: int = 0
    no: int = 0
    abstain: int = 0
    agenda_item: str = ""
    created_at: datetime = field(default_factory=now)

    @property
    def result(self) -> str:
        if self.yes > self.no:
            return VoteResult.ACCEPTED
        if self.no > self.yes:
            return VoteResult.REJECTED
        return VoteResult.TIE

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "yes": self.yes, "no": self.no,
                "abstain": self.abstain, "result": self.result,
                "agenda_item": self.agenda_item, "created_at": dt_to_str(self.created_at)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Vote":
        return cls(topic=data["topic"], yes=int(data.get("yes", 0)), no=int(data.get("no", 0)),
                   abstain=int(data.get("abstain", 0)), agenda_item=data.get("agenda_item") or "",
                   created_at=str_to_dt(data.get("created_at")) or now())


@dataclass
class SpeakerTurn:
    speaker: str
    started_at: datetime
    ended_at: datetime | None = None
    agenda_item: str = ""
    # Program yeniden açıldığında aynı kişinin sözü sürdürülür: yeni söz alma sayılmaz (Faz 5c.2 canlı düzeltme)
    resumed: bool = False

    @property
    def duration_seconds(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        data = {"speaker": self.speaker, "started_at": dt_to_str(self.started_at),
                "ended_at": dt_to_str(self.ended_at), "agenda_item": self.agenda_item}
        if self.resumed:
            data["resumed"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpeakerTurn":
        started = str_to_dt(data.get("started_at"))
        if started is None:
            raise ValueError("SpeakerTurn.started_at boş olamaz")
        return cls(speaker=data["speaker"], started_at=started,
                   ended_at=str_to_dt(data.get("ended_at")), agenda_item=data.get("agenda_item") or "",
                   resumed=bool(data.get("resumed")))


@dataclass
class TranscriptEntry:
    timestamp: datetime
    speaker: str
    text: str
    agenda_item: str = ""
    # Faz 5c.2 canlı düzeltme: "" = toplantı konuşması; TranscriptKind.COMMAND_LIKE / EXECUTED = "Jarvis" denmeden
    # söylenmiş komut kalıbı. Kayıt silinmez; analiz ve konuşma istatistiklerinde konuşma sayılmaz.
    kind: str = ""
    # Sesli girişte konuşma bölümünün süresi (sn); klavye kipinde yoktur
    duration_s: float | None = None

    @property
    def is_speech(self) -> bool:
        return not self.kind

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"timestamp": dt_to_str(self.timestamp), "speaker": self.speaker,
                                "text": self.text, "agenda_item": self.agenda_item}
        if self.kind:  # eski kayıtlarla birebir uyum için yalnızca doluysa yazılır
            data["kind"] = self.kind
        if self.duration_s is not None:
            data["duration_s"] = round(self.duration_s, 2)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptEntry":
        duration = data.get("duration_s")
        return cls(timestamp=str_to_dt(data.get("timestamp")) or now(), speaker=data.get("speaker") or "",
                   text=data["text"], agenda_item=data.get("agenda_item") or "", kind=data.get("kind") or "",
                   duration_s=float(duration) if isinstance(duration, (int, float)) else None)


class TranscriptKind:
    COMMAND_LIKE = "komut_benzeri"  # hitapsız ama komut kalıbında; işlenmedi
    EXECUTED = "komut"  # hitapsız duyuldu, kullanıcı onayıyla komut olarak işlendi
    LABELS = {COMMAND_LIKE: "komut gibi duyuldu, işlenmedi", EXECUTED: "komut olarak işlendi"}


class DraftStatus:
    DRAFT = "taslak"
    APPROVED = "onaylandı"
    REJECTED = "reddedildi"


class DraftKind:
    DECISION = "karar"
    ACTION = "görev"
    SUGGESTION = "öneri"
    QUESTION = "soru"
    OPEN_ISSUE = "açık konu"


@dataclass
class Evidence:
    """Taslak öğenin dayandığı transkript satırı."""

    line: str  # "L12"
    timestamp: datetime | None
    speaker: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "timestamp": dt_to_str(self.timestamp), "speaker": self.speaker,
                "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(line=str(data.get("line") or ""), timestamp=str_to_dt(data.get("timestamp")),
                   speaker=data.get("speaker") or "", text=data.get("text") or "")


@dataclass
class AiDraft:
    """Normal konuşmadan çıkarılan aday (karar/görev/öneri/soru).

    Taslak resmî kayıt DEĞİLDİR: PDF'in karar/görev tablolarına ve devreden görevlere
    yalnızca kullanıcı "Jarvis, taslak N onayla" dedikten sonra girer.
    """

    number: int
    kind: str
    text: str
    evidence: list[Evidence] = field(default_factory=list)
    owner: str = ""
    due_date: date | None = None
    due_text: str = ""
    agenda_item: str = ""
    status: str = DraftStatus.DRAFT
    source: str = ""  # "llm:qwen3:4b" veya "kural"
    notes: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now)
    record_ref: str = ""  # onaylanınca oluşturulan kayıt (görev kimliği / karar sırası)

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "kind": self.kind, "text": self.text,
                "evidence": [e.to_dict() for e in self.evidence], "owner": self.owner,
                "due_date": date_to_str(self.due_date), "due_text": self.due_text, "agenda_item": self.agenda_item,
                "status": self.status, "source": self.source, "notes": list(self.notes),
                "created_at": dt_to_str(self.created_at), "record_ref": self.record_ref}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AiDraft":
        return cls(number=int(data.get("number") or 0), kind=data.get("kind") or DraftKind.SUGGESTION,
                   text=data.get("text") or "", evidence=[Evidence.from_dict(e) for e in data.get("evidence") or []],
                   owner=data.get("owner") or "", due_date=str_to_date(data.get("due_date")),
                   due_text=data.get("due_text") or "", agenda_item=data.get("agenda_item") or "",
                   status=data.get("status") or DraftStatus.DRAFT, source=data.get("source") or "",
                   notes=list(data.get("notes") or []), created_at=str_to_dt(data.get("created_at")) or now(),
                   record_ref=data.get("record_ref") or "")


@dataclass
class Meeting:
    title: str
    started_at: datetime = field(default_factory=now)
    id: str = field(default_factory=lambda: now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    ended_at: datetime | None = None
    chair: str = ""
    location: str = ""
    participants: list[Participant] = field(default_factory=list)
    agenda: list[AgendaItem] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    votes: list[Vote] = field(default_factory=list)
    parking_lot: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    speaker_turns: list[SpeakerTurn] = field(default_factory=list)
    transcript: list[TranscriptEntry] = field(default_factory=list)
    previous_meeting_id: str | None = None
    next_meeting_date: date | None = None
    summary: str = ""
    # PDF modelinde olmayan, işletim için gerekli ek alanlar
    status: str = MeetingStatus.ACTIVE
    current_item_index: int | None = None
    active_speaker: str | None = None
    is_demo: bool = False
    minutes_pdf: str | None = None
    email_status: dict[str, Any] = field(default_factory=dict)
    load_warnings: list[str] = field(default_factory=list)
    # Önceki toplantılardan gelip bu toplantıda tamamlanan görevlerin kimlikleri (G-13)
    carryover_completed: list[str] = field(default_factory=list)
    # "Geri al" komutu için son eklenen kayıt: {"kind": "decision"|"action"|..., "ref": ...}
    last_record: dict[str, Any] | None = None
    # Yapay zekâ (yerel LLM veya kural tabanlı) analiz çıktıları: onaylanmamış taslaklar ve özet taslağı
    ai_drafts: list[AiDraft] = field(default_factory=list)
    ai_summary: dict[str, Any] = field(default_factory=dict)
    # Bu toplantının listesinden çıkarılan (yanlış eklenmiş / birleştirilmiş) kayıtlar. Silinmez; yoklama, e-posta
    # ve eşleştirmede kullanılmaz. Rehber (participants.json) ve geçmiş toplantılar etkilenmez.
    removed_participants: list[Participant] = field(default_factory=list)
    # Faz 6: sonraki toplantının saati ("14:00"; söylenmediyse boş — saat uydurulmaz) ve takvim daveti kaydı
    # {"path", "uid", "sequence", "fingerprint", "created_at", "attach"}. Yalnızca doluysa yazılır.
    next_meeting_time: str = ""
    next_meeting_invite: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == MeetingStatus.ACTIVE

    @property
    def current_item(self) -> AgendaItem | None:
        if self.current_item_index is None:
            return None
        if 0 <= self.current_item_index < len(self.agenda):
            return self.agenda[self.current_item_index]
        return None

    @property
    def present_participants(self) -> list[Participant]:
        return [p for p in self.participants if p.present]

    @property
    def absent_participants(self) -> list[Participant]:
        return [p for p in self.participants if not p.present]

    def find_participant(self, name: str) -> Participant | None:
        for participant in self.participants:
            if participant.name == name:
                return participant
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "started_at": dt_to_str(self.started_at),
            "ended_at": dt_to_str(self.ended_at),
            "chair": self.chair,
            "location": self.location,
            "participants": [p.to_dict() for p in self.participants],
            "agenda": [a.to_dict() for a in self.agenda],
            "decisions": [d.to_dict() for d in self.decisions],
            "action_items": [a.to_dict() for a in self.action_items],
            "votes": [v.to_dict() for v in self.votes],
            "parking_lot": list(self.parking_lot),
            "notes": list(self.notes),
            "speaker_turns": [t.to_dict() for t in self.speaker_turns],
            "transcript": [t.to_dict() for t in self.transcript],
            "previous_meeting_id": self.previous_meeting_id,
            "next_meeting_date": date_to_str(self.next_meeting_date),
            "summary": self.summary,
            "status": self.status,
            "current_item_index": self.current_item_index,
            "active_speaker": self.active_speaker,
            "is_demo": self.is_demo,
            "minutes_pdf": self.minutes_pdf,
            "email_status": dict(self.email_status),
            "load_warnings": list(self.load_warnings),
            "carryover_completed": list(self.carryover_completed),
            "last_record": dict(self.last_record) if self.last_record else None,
            "ai_drafts": [d.to_dict() for d in self.ai_drafts],
            "ai_summary": dict(self.ai_summary),
            **({"removed_participants": [p.to_dict() for p in self.removed_participants]}
               if self.removed_participants else {}),
            **({"next_meeting_time": self.next_meeting_time} if self.next_meeting_time else {}),
            **({"next_meeting_invite": dict(self.next_meeting_invite)} if self.next_meeting_invite else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Meeting":
        return cls(
            id=data["id"],
            title=data["title"],
            started_at=str_to_dt(data.get("started_at")) or now(),
            ended_at=str_to_dt(data.get("ended_at")),
            chair=data.get("chair") or "",
            location=data.get("location") or "",
            participants=[Participant.from_dict(p) for p in data.get("participants") or []],
            agenda=[AgendaItem.from_dict(a) for a in data.get("agenda") or []],
            decisions=[Decision.from_dict(d) for d in data.get("decisions") or []],
            action_items=[ActionItem.from_dict(a) for a in data.get("action_items") or []],
            votes=[Vote.from_dict(v) for v in data.get("votes") or []],
            parking_lot=list(data.get("parking_lot") or []),
            notes=list(data.get("notes") or []),
            speaker_turns=[SpeakerTurn.from_dict(t) for t in data.get("speaker_turns") or []],
            transcript=[TranscriptEntry.from_dict(t) for t in data.get("transcript") or []],
            previous_meeting_id=data.get("previous_meeting_id"),
            next_meeting_date=str_to_date(data.get("next_meeting_date")),
            summary=data.get("summary") or "",
            status=data.get("status") or MeetingStatus.ACTIVE,
            current_item_index=data.get("current_item_index"),
            active_speaker=data.get("active_speaker"),
            is_demo=bool(data.get("is_demo", False)),
            minutes_pdf=data.get("minutes_pdf"),
            email_status=dict(data.get("email_status") or {}),
            load_warnings=list(data.get("load_warnings") or []),
            carryover_completed=list(data.get("carryover_completed") or []),
            last_record=data.get("last_record"),
            ai_drafts=[AiDraft.from_dict(d) for d in data.get("ai_drafts") or []],
            ai_summary=dict(data.get("ai_summary") or {}),
            removed_participants=[Participant.from_dict(p) for p in data.get("removed_participants") or []],
            next_meeting_time=str(data.get("next_meeting_time") or ""),
            next_meeting_invite=dict(data.get("next_meeting_invite") or {}),
        )
