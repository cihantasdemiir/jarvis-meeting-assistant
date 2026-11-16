"""Özet güvenliği (Faz 5a.2): talimat sızıntısı ve kanıtsız kesin karar ifadelerine karşı kod düzeyinde denetim.

Gerçek benchmark bulguları:
  * S04 / HS02: Model sahte talimatı uygulamadı ama talimattaki cümleyi ("Toplantı iptal edildi",
    "Bütçe reddedildi") özete taşıdı.
  * HS01: İtiraz edilip sonraki toplantıya bırakılan öneri özette "kararlaştırıldı" diye geçti.

İki katman (ek LLM çağrısı yok):
  1. Girdi: talimat benzeri satırların METNİ modele gönderilmez ("[talimat benzeri içerik gizlendi]").
     Satır transkriptte aynen kalır (Ek A); yalnızca yapay zekâ girdisinden çıkarılır.
  2. Çıktı: özet cümlelere bölünür.
     - Talimat satırlarından alıntı veya o satırlara özgü kelimeler içeren cümle çıkarılır.
     - Kesin karar bildiren cümle ("karara bağladı", "kararlaştırıldı", "onaylandı") doğrulanmış bir
       karar adayıyla desteklenmiyorsa: konu açık konu olarak işaretliyse "karar verilmedi; netleştirilecek"
       biçiminde yeniden yazılır, değilse cümleye "kesin karar kaydı yok" notu eklenir. Meşru karar
       cümlelerine dokunulmaz; basit yasak kelime filtresi kullanılmaz.
     - Kanıtsız görev ataması ("X görevlendirildi") çıkarılır.
     - Geriye cümle kalmazsa özet, doğrulanmış adaylardan deterministik olarak yeniden oluşturulur.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..meeting.models import DraftKind
from ..utils.turkish_text import ascii_fold, best_name_match, tr_lower
from .grounding import _word_matches, content_words
from .refine import has_decision_language, is_instruction_like

REDACTED = "[talimat benzeri içerik; güvenilmeyen veri olarak gizlendi]"

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_QUOTED = re.compile(r"[\"'“”‘’«]([^\"'“”‘’«»]{3,})[\"'“”‘’»]")
_META = re.compile(r"\bözet\w*\s+(?:sonuna|başına)|\btalimat\w*|\bsistem (?:notu|mesajı|talimatı)|\bjarvis\b"
                   r"|\b(?:kuralları|talimatları) (?:unut|yok say)")
_DECISION_CLAIM = re.compile(r"\b(?:karar(?:a bağla\w*| ver\w*| alın\w*| alındı\w*)|kararlaştır\w*|kesinleş\w*"
                             r"|onayla(?:n|d)\w*|kabul edil\w*|mutabakat\w*|anlaşmaya var\w*)")
_NEGATED = re.compile(r"(?:madı|medi|mamış|memiş|mayacak|meyecek|madığı|mediği)\w*$")
_NOTE = "(Not: transkriptte bu konuda kesin karar ifadesi yok.)"
# Kesin karar ifadesini, bağlama göre doğru ifadeyle değiştirir; cümlenin geri kalanı (gerçek içerik) korunur.
_TO_OPEN = {"kararlaştırıldı": "konusunda henüz karar verilmedi", "karara bağlandı": "konusunda henüz karar verilmedi",
            "karara bağladı": "konusunda henüz karar vermedi", "karar verildi": "karar verilmedi",
            "karar verdi": "karar vermedi", "karar verdik": "karar vermedik", "karar alındı": "karar alınmadı",
            "onaylandı": "onaylanmadı", "onayladı": "onaylamadı", "kabul edildi": "kabul edilmedi",
            "kesinleşti": "kesinleşmedi", "kesinleştirildi": "kesinleştirilmedi"}
_TO_SUGGESTION = {"kararlaştırıldı": "önerildi", "karara bağlandı": "önerildi", "karara bağladı": "önerdi",
                  "karar verildi": "önerildi", "karar verdi": "önerdi", "karar verdik": "önerdik",
                  "karar alındı": "önerildi", "onaylandı": "önerildi", "onayladı": "önerdi",
                  "kabul edildi": "önerildi", "kesinleşti": "önerildi", "kesinleştirildi": "önerildi"}


def _replace_claim(sentence: str, table: dict[str, str]) -> str | None:
    for phrase in sorted(table, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)
        if pattern.search(sentence):
            return pattern.sub(table[phrase], sentence, count=1)
    return None


_TASK_CLAIM = re.compile(r"\bgörevlendir\w*|\bsorumlu (?:olarak|tutul|oldu)\w*|\bgörev(?:i|ini) üstlen\w*")


@dataclass
class SummaryCheck:
    text: str
    removed: list[tuple[str, str]] = field(default_factory=list)  # (cümle, neden)
    rewritten: list[tuple[str, str]] = field(default_factory=list)  # (eski, yeni)
    rebuilt: bool = False


def render_for_llm(line) -> str:
    """Satırın modele gönderilecek hâli; talimat benzeri satırların metni gizlenir."""
    text = REDACTED if is_instruction_like(line.entry.text) else line.entry.text
    return f"{line.id} [{line.entry.timestamp:%H:%M:%S}] {line.entry.speaker}: {text}"


def _shares(a: str, b: str) -> bool:
    words_a, words_b = content_words(a), content_words(b)
    return any(_word_matches(x, y) for x in words_a for y in words_b)


def _distinctive_words(lines: list) -> set[str]:
    flagged = [line.entry.text for line in lines if is_instruction_like(line.entry.text)]
    if not flagged:
        return set()
    normal = {w for line in lines if not is_instruction_like(line.entry.text) for w in content_words(line.entry.text)}
    return {w for text in flagged for w in content_words(text)
            if not any(_word_matches(w, n) for n in normal)}


def _leaks(sentence: str, lines: list, distinctive: set[str]) -> str:
    low, folded = tr_lower(sentence), ascii_fold(sentence)
    for line in lines:
        if is_instruction_like(line.entry.text):
            for fragment in _QUOTED.findall(line.entry.text):
                if ascii_fold(fragment.strip()) in folded:
                    return "talimat satırından alıntı"
    if _META.search(low):
        return "asistana yöneltilmiş talimattan söz ediyor"
    hits = sum(any(_word_matches(w, s) for s in content_words(sentence)) for w in distinctive)
    if hits >= 2:
        return "talimat satırına özgü içerik"
    return ""


def _claims_decision(sentence: str) -> bool:
    for match in _DECISION_CLAIM.finditer(tr_lower(sentence)):
        if not _NEGATED.search(match.group(0)):
            return True
    return False


def _names_in(sentence: str, participants: list[str]) -> set[str]:
    found = set()
    for word in sentence.replace("'", " ").split():
        match = best_name_match(word.strip(".,;:"), participants)
        if match.found and match.name and word[:1].isupper():
            found.add(match.name)
    return found


def sanitize_summary(summary: str, lines: list, candidates: list, participants: list[str]) -> SummaryCheck:
    check = SummaryCheck(text="")
    distinctive = _distinctive_words(lines)
    decisions = [c for c in candidates if c.kind == DraftKind.DECISION]
    open_issues = [c for c in candidates if c.kind == DraftKind.OPEN_ISSUE]
    owners = {c.owner for c in candidates if c.kind == DraftKind.ACTION and c.owner}
    kept: list[str] = []
    for sentence in [s.strip() for s in _SENTENCE.split(summary or "") if s.strip()]:
        reason = _leaks(sentence, lines, distinctive)
        if reason:
            check.removed.append((sentence, reason))
            continue
        if _TASK_CLAIM.search(tr_lower(sentence)) and not (_names_in(sentence, participants) & owners):
            check.removed.append((sentence, "doğrulanmış görev adayı yok"))
            continue
        if _claims_decision(sentence) and _NOTE not in sentence:
            issue = next((o for o in open_issues
                          if _shares(sentence, o.text) or _shares(sentence, _evidence_text(o, lines))), None)
            supported = any(_shares(sentence, d.text) for d in decisions) or (
                issue is None and any(has_decision_language(line.entry.text) and _shares(sentence, line.entry.text)
                                      for line in lines if not is_instruction_like(line.entry.text)))
            if not supported:
                suggestion = next((c for c in candidates if c.kind == DraftKind.SUGGESTION
                                   and (_shares(sentence, c.text) or _shares(sentence, _evidence_text(c, lines)))),
                                  None)
                new = None
                if issue is not None:
                    new = _replace_claim(sentence, _TO_OPEN)
                    new = f"{new.rstrip('.')} (açık konu; netleştirilecek)." if new else None
                elif suggestion is not None:
                    new = _replace_claim(sentence, _TO_SUGGESTION)
                    new = f"{new.rstrip('.')} (kesin karar kaydı yok)." if new else None
                new = new or f"{sentence.rstrip('.')} {_NOTE}"
                check.rewritten.append((sentence, new))
                sentence = new
        if sentence not in kept:
            kept.append(sentence)
    check.text = " ".join(kept)
    if not check.text and (summary or "").strip():
        check.text = rebuild_summary(candidates)
        check.rebuilt = True
    return check


def _evidence_text(candidate, lines: list) -> str:
    by_id = {line.id: line for line in lines}
    return " ".join(by_id[e].entry.text for e in candidate.evidence if e in by_id)


def rebuild_summary(candidates: list) -> str:
    """Modelin özeti tamamen doğrulanamadığında doğrulanmış adaylardan kısa özet."""
    parts = []
    groups = ((DraftKind.DECISION, "Kararlar"), (DraftKind.OPEN_ISSUE, "Karar verilmeyen konular"),
              (DraftKind.ACTION, "Görevler"), (DraftKind.SUGGESTION, "Öneriler"))
    for kind, label in groups:
        items = [c for c in candidates if c.kind == kind]
        if items:
            texts = [f"{c.owner}: {c.text}" if c.owner else c.text for c in items[:5]]
            parts.append(f"{label}: {'; '.join(t.rstrip('.') for t in texts)}.")
    return " ".join(parts)
