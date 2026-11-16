"""Katılımcı düzeltmeleri (Faz 5c.2 katılımcı düzeltmesi).

Üç ayrı işlem; birbirinin yerine kullanılmaz:
  A. Yoklama düzeltmesi (`absent`): kişi bu toplantının listesinde kalır, "katılmadı" olur. Konuşma ve
     görev kayıtları olduğu gibi kalır (o anda yaşananların kaydıdır).
  B. Listeden çıkarma (`remove`): yanlışlıkla oluşmuş kayıt bu toplantının listesinden çıkarılır. Kayıt silinmez,
     `Meeting.removed_participants` içine taşınır. Kişiye bağlı görev, konuşma, söz veya e-posta kaydı varsa
     YAPILMAZ: önce A veya C önerilir (ilişkili kaydı sessizce yok etmemek için).
  C. Ad düzeltme (`rename`): toplantı sırasında eklenmiş, adı yanlış alınmış kayıt düzeltilir. Doğru ad listede
     varsa iki kayıt birleştirilir; bu toplantıdaki söz, transkript ve görev ilişkileri doğru kişiye taşınır.
     Rehberden (participants.json) gelen kişinin adı burada değiştirilmez.

Hepsi yalnızca AÇIK toplantıyı değiştirir; geçmiş toplantılar, oluşmuş PDF'ler ve rehber dosyası değişmez.
`plan()` hiçbir şeyi değiştirmez; `apply()` değişiklikleri bellekte uygular ve geri alma bilgisi döndürür.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..utils.turkish_text import best_name_match, plausible_person_name, strip_honorifics, strip_name_suffix, tr_title
from .models import Meeting, Participant

OPS = ("absent", "remove", "rename")
REASON_WRONG = "yanlış kayıt"
REASON_MERGED = "ad düzeltmesiyle birleştirildi"


@dataclass
class Relations:
    actions: int = 0
    turns: int = 0
    transcript: int = 0
    active_speaker: bool = False
    emailed: bool = False

    @property
    def any(self) -> bool:
        return bool(self.actions or self.turns or self.transcript or self.active_speaker or self.emailed)

    def describe(self) -> str:
        parts = []
        if self.actions:
            parts.append(f"{self.actions} görev")
        if self.turns:
            parts.append(f"{self.turns} söz kaydı")
        if self.transcript:
            parts.append(f"{self.transcript} konuşma kaydı")
        if self.active_speaker:
            parts.append("şu anda söz onda")
        if self.emailed:
            parts.append("tutanak e-postası gönderilmiş")
        return ", ".join(parts)


@dataclass
class Plan:
    ok: bool
    message: str
    op: str = ""
    name: str = ""  # çözümlenmiş mevcut ad
    new_name: str = ""  # rename: hedef ad
    merge: bool = False  # rename: hedef listede var, kayıtlar birleşir
    candidates: list[str] = field(default_factory=list)  # belirsiz eşleşme
    relations: Relations = field(default_factory=Relations)


def clean_spoken(name: str) -> str:
    words = [strip_name_suffix(w.strip(" .,;:!?")) for w in strip_honorifics(name or "").split()]
    return " ".join(w for w in words if w)


def relations(meeting: Meeting, name: str) -> Relations:
    person = meeting.find_participant(name)
    email = (person.email or "").lower() if person else ""
    sent = {str(k).lower() for k in (meeting.email_status or {}).get("sent_to", []) or []}
    return Relations(
        actions=sum(1 for a in meeting.action_items if a.owner == name),
        turns=sum(1 for t in meeting.speaker_turns if t.speaker == name),
        transcript=sum(1 for t in meeting.transcript if t.speaker == name),
        active_speaker=meeting.active_speaker == name,
        emailed=bool(email and email in sent),
    )


def _resolve(meeting: Meeting, spoken: str, exclude: str | None = None) -> tuple[str | None, list[str]]:
    names = [p.name for p in meeting.participants if p.name != exclude]
    match = best_name_match(spoken, names)
    if match.is_ambiguous:
        return None, list(match.candidates)
    return (match.name if match.found else None), []


def plan(meeting: Meeting, op: str, spoken: str, new_name: str = "") -> Plan:
    """İşlemi doğrular ve onay mesajını hazırlar. Toplantı DEĞİŞMEZ."""
    if op not in OPS:
        return Plan(False, "Bilinmeyen katılımcı işlemi.")
    query = clean_spoken(spoken)
    if not query:
        return Plan(False, "Kişinin adı anlaşılamadı.", op)
    name, candidates = _resolve(meeting, query)
    if candidates:
        return Plan(False, f"'{query}' birden fazla kişiyle eşleşiyor: {', '.join(candidates)}. Tam adı söyleyin.",
                    op, candidates=candidates)
    if name is None:
        return Plan(False, f"'{query}' bu toplantının katılımcı listesinde yok; hiçbir şey değiştirilmedi.", op)
    person = meeting.find_participant(name)
    assert person is not None
    rel = relations(meeting, name)
    if op == "absent":
        if not person.present:
            return Plan(False, f"{name} zaten katılmadı olarak görünüyor; değişiklik gerekmiyor.", op, name)
        notes = ""
        if rel.active_speaker:
            notes += " Söz şu anda onda; söz kapatılacak."
        if rel.turns or rel.transcript or rel.actions:
            notes += f" Kayıtları ({rel.describe()}) olduğu gibi korunur."
        return Plan(True, f"{name} yoklamadan çıkarılacak: listede kalır, «katılmadı» olarak işaretlenir.{notes} "
                          "Onaylıyor musunuz?", op, name, relations=rel)
    if op == "remove":
        if rel.any:
            return Plan(False, f"{name} kaydı bu toplantıdan kaldırılamaz: {rel.describe()} var ve bunlar "
                               f"sessizce silinmez. Yalnızca yoklamayı düzeltmek için «Jarvis, {name} yoklamadan "
                               f"çıkar», ad yanlış alındıysa «Jarvis, {name} olarak kaydettiğin kişinin adı aslında "
                               "…» deyin.", op, name, relations=rel)
        where = ("toplantı sırasında eklenmiş bu kayıt" if person.added_during_meeting
                 else "kişi rehberde kalır, yalnızca bu toplantının listesinden")
        return Plan(True, f"{name} bu toplantının katılımcı listesinden çıkarılacak ({where}); kayıt silinmez, "
                          "«geri al» ile geri getirilebilir. Onaylıyor musunuz?", op, name, relations=rel)
    # rename
    target_query = clean_spoken(new_name)
    if not target_query:
        return Plan(False, "Doğru ad anlaşılamadı.", op, name)
    if not person.added_during_meeting:
        return Plan(False, f"{name} rehberdeki (katılımcı dosyasındaki) bir kişi; adı toplantı içinden "
                           "değiştirilmez. Kalıcı düzeltme için katılımcı dosyasını güncelleyin.", op, name)
    target, candidates = _resolve(meeting, target_query, exclude=name)
    if candidates:
        return Plan(False, f"'{target_query}' birden fazla kişiyle eşleşiyor: {', '.join(candidates)}. "
                           "Tam adı söyleyin.", op, name, candidates=candidates, new_name=target_query)
    affected = f" Bu toplantıdaki kayıtları ({rel.describe()}) {{}} adına taşınır." if rel.any else ""
    if target is not None:
        if target == name:
            return Plan(False, f"Kayıt zaten {name} adında.", op, name)
        return Plan(True, f"'{name}' kaydı listedeki {target} ile birleştirilecek; yanlış kayıt listeden "
                          f"çıkarılır (silinmez).{affected.format(target)} Onaylıyor musunuz?", op, name,
                    new_name=target, merge=True, relations=rel)
    if not plausible_person_name(target_query):
        return Plan(False, f"'{target_query}' bir kişi adı olarak anlaşılamadı; hiçbir şey değiştirilmedi.", op, name)
    fixed = tr_title(target_query)
    return Plan(True, f"'{name}' kaydının adı '{fixed}' olarak düzeltilecek.{affected.format(fixed)} "
                      "Onaylıyor musunuz?", op, name, new_name=fixed, relations=rel)


def apply(meeting: Meeting, p: Plan, at: datetime) -> tuple[str, dict[str, Any]]:
    """Onaylanmış planı uygular; (mesaj, geri alma bilgisi) döndürür. Önce `plan()` ile yeniden doğrulanmalıdır."""
    person = meeting.find_participant(p.name)
    assert person is not None
    if p.op == "absent":
        closed_turn = False
        if meeting.active_speaker == p.name:
            for turn in meeting.speaker_turns:
                if turn.ended_at is None:
                    turn.ended_at = max(at, turn.started_at)
            meeting.active_speaker = None
            closed_turn = True
        person.present = False
        note = " Söz kapatıldı." if closed_turn else ""
        return f"{p.name} yoklamadan çıkarıldı; listede «katılmadı» olarak duruyor.{note}", {
            "op": "absent", "name": p.name}
    if p.op == "remove":
        index = meeting.participants.index(person)
        meeting.participants.pop(index)
        person.removed_reason, person.removed_at = REASON_WRONG, at
        meeting.removed_participants.append(person)
        return f"{p.name} bu toplantının katılımcı listesinden çıkarıldı.", {
            "op": "remove", "name": p.name, "index": index}
    # rename / merge: bu toplantıdaki ilişkiler yeni ada taşınır
    new = p.new_name
    moved = {
        "turns": [i for i, t in enumerate(meeting.speaker_turns) if t.speaker == p.name],
        "transcript": [i for i, t in enumerate(meeting.transcript) if t.speaker == p.name],
        "actions": [a.id for a in meeting.action_items if a.owner == p.name],
        "active": meeting.active_speaker == p.name,
    }
    for i in moved["turns"]:
        meeting.speaker_turns[i].speaker = new
    for i in moved["transcript"]:
        meeting.transcript[i].speaker = new
    for action in meeting.action_items:
        if action.id in moved["actions"]:
            action.owner = new
    if moved["active"]:
        meeting.active_speaker = new
    undo: dict[str, Any] = {"op": "rename", "name": p.name, "new_name": new, "merge": p.merge, **moved}
    if p.merge:
        target = meeting.find_participant(new)
        assert target is not None
        undo["target_present"] = target.present
        target.present = target.present or person.present
        index = meeting.participants.index(person)
        meeting.participants.pop(index)
        person.removed_reason, person.removed_at, person.merged_into = REASON_MERGED, at, new
        meeting.removed_participants.append(person)
        undo["index"] = index
        return f"'{p.name}' kaydı {new} ile birleştirildi; yanlış kayıt listeden çıkarıldı.", undo
    undo["former_name"] = person.former_name
    person.former_name = person.former_name or p.name
    person.name = new
    return f"Ad düzeltildi: '{p.name}' → '{new}'.", undo


def undo(meeting: Meeting, info: dict[str, Any]) -> str:
    """`apply()` sonrasını geri alır (yalnızca aynı toplantı içinde, son işlem için)."""
    op, name = info.get("op"), info.get("name", "")
    if op == "absent":
        person = meeting.find_participant(name)
        if person is None:
            return ""
        person.present = True
        return f"{name} yeniden katıldı olarak işaretlendi"
    if op == "remove":
        person = _take_removed(meeting, name)
        if person is None:
            return ""
        meeting.participants.insert(min(info.get("index", len(meeting.participants)), len(meeting.participants)),
                                    person)
        return f"{name} katılımcı listesine geri alındı"
    if op == "rename":
        new = info.get("new_name", "")
        for i in info.get("turns", []):
            if i < len(meeting.speaker_turns) and meeting.speaker_turns[i].speaker == new:
                meeting.speaker_turns[i].speaker = name
        for i in info.get("transcript", []):
            if i < len(meeting.transcript) and meeting.transcript[i].speaker == new:
                meeting.transcript[i].speaker = name
        for action in meeting.action_items:
            if action.id in info.get("actions", []):
                action.owner = name
        if info.get("active") and meeting.active_speaker == new:
            meeting.active_speaker = name
        if info.get("merge"):
            person = _take_removed(meeting, name)
            target = meeting.find_participant(new)
            if person is None:
                return ""
            if target is not None:
                target.present = bool(info.get("target_present"))
            meeting.participants.insert(min(info.get("index", 0), len(meeting.participants)), person)
            return f"'{name}' ile {new} birleştirmesi geri alındı"
        person = meeting.find_participant(new)
        if person is None:
            return ""
        person.name, person.former_name = name, info.get("former_name", "")
        return f"Ad düzeltmesi geri alındı: '{new}' → '{name}'"
    return ""


def _take_removed(meeting: Meeting, name: str) -> Participant | None:
    for index in range(len(meeting.removed_participants) - 1, -1, -1):
        person = meeting.removed_participants[index]
        if person.name == name:
            meeting.removed_participants.pop(index)
            person.removed_reason, person.removed_at, person.merged_into = "", None, ""
            return person
    return None


def roster_text(meeting: Meeting) -> str:
    """"Katılımcı listesini oku": davetli/aday liste, katılanlar, misafirler, listeden çıkarılanlar."""
    if not meeting.participants:
        return "Bu toplantının katılımcı listesi boş."
    present = [p.name for p in meeting.participants if p.present]
    absent = [p.name for p in meeting.participants if not p.present]
    guests = [p.name for p in meeting.participants if p.added_during_meeting]
    parts = [f"Bu toplantının listesinde {len(meeting.participants)} kişi var."]
    parts.append(f"Katıldı ({len(present)}): {', '.join(present)}." if present else "Henüz yoklama alınmadı.")
    if absent:
        parts.append(f"Katılmadı ({len(absent)}): {', '.join(absent)}.")
    if guests:
        parts.append(f"Toplantı sırasında eklenenler: {', '.join(guests)}.")
    renamed = [f"{p.former_name} → {p.name}" for p in meeting.participants if p.former_name]
    if renamed:
        parts.append(f"Ad düzeltmeleri: {', '.join(renamed)}.")
    if meeting.removed_participants:
        parts.append("Listeden çıkarılan kayıtlar: " + ", ".join(removed_label(p) for p in meeting.removed_participants)
                     + ".")
    return " ".join(parts)


def removed_label(person: Participant) -> str:
    reason = f"{person.merged_into} ile birleştirildi" if person.merged_into else (person.removed_reason or "çıkarıldı")
    return f"{person.name} ({reason})"
