"""Görev sorumlusunun kaynak transkriptten belirlenmesi (Faz 5a.2).

Gerçek benchmark bulgusu (HE06, HE12): model sorumlu olarak satırı SÖYLEYEN kişiyi döndürdü
("Murat Şen: Selim sunumu hazırlasın" → sorumlu Murat). Eski doğrulama, "ben hazırlayacağım" durumunu
desteklemek için konuşmacıyı koşulsuz kabul ediyordu. Bu modül sorumluyu modelden değil cümleden çıkarır:

  * Cümle virgül/noktalı virgülle yan cümlelere ayrılır ("Elif afişleri hazırlasın, Burak da baskıyı
    takip etsin" → iki görev). Her yan cümlede atama dili (emir kipi, gereklilik, gelecek zaman) aranır.
  * Yan cümlede katılımcı adı geçiyorsa sorumlu odur. Ad birden çok katılımcıya uyuyorsa (iki Elif)
    sorumlu BOŞ kalır ve belirsizlik notu düşülür; model ne derse desin seçim yapılmaz.
  * Konuşmacı yalnızca gerçek birinci şahıs taahhüdünde ("ben hazırlayacağım", "bunu üstleniyorum")
    ve yan cümlede başka bir kişi görevlendirilmiyorsa sorumlu olur.
  * Modelin önerdiği isim yalnızca cümlede geçen kelimeleriyle çözülür (yönlendiricideki kuralın aynısı).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..meeting.action_extractor import extract_action
from ..utils.turkish_text import best_name_match, strip_honorifics, strip_name_suffix, tr_lower
from .grounding import grounded_ratio, phrase_in

_CLAUSE_SPLIT = re.compile(r"\s*[;,]\s*|\s+ayrıca\s+")
_IMPERATIVE = re.compile(r"\b\w{2,}(?:sın|sin|sun|sün|sınlar|sinler|sunlar|sünler)\b")
_NECESSITY = re.compile(r"\b\w+(?:malı|meli)\b|\b(?:lazım|gerek|gerekiyor)\b")
_FUTURE_3P = re.compile(r"\b\w+(?:acak|ecek|acaklar|ecekler)\b")
# Gerçek birinci şahıs taahhüdü: "ben", "-acağım/-eceğim", "-ayım/-eyim", "üstleniyorum/üstlendim"
_FIRST_PERSON = re.compile(r"\bben\b|\b\w+(?:acağım|eceğim|ayım|eyim)\b|\büstlen(?:iyorum|dim|eceğim)\b")
# Aktarılan söz: "Ayşe dedi ki Mehmet yapsın" — sözü aktaran değil, aktarılan cümlenin öznesi sorumludur
_REPORTED = re.compile(r"\b(?:dedi|diyor|söyledi|söylüyor|istedi)\b(?:\s+ki\b)?")


# Ad çıkarılınca baştaki bağlaç kalmasın: "Zeynep de ekibe duyursun" → "ekibe duyursun"
_LEADING_PARTICLE = re.compile(r"^(?:de|da|ise|ile|ayrıca)\s+", re.IGNORECASE)


@dataclass
class Assignment:
    clause: str
    owner: str = ""  # katılımcı listesindeki tek eşleşme
    candidates: list[str] = field(default_factory=list)  # belirsiz ad: birden çok katılımcı
    spoken: str = ""  # cümlede geçen ad (liste dışı olabilir)
    first_person: bool = False
    due: date | None = None
    due_text: str = ""
    description: str = ""


def split_clauses(text: str) -> list[str]:
    return [c.strip(" .") for c in _CLAUSE_SPLIT.split(text or "") if c and c.strip(" .")]


def _has_assignment_language(low: str) -> bool:
    return bool(_IMPERATIVE.search(low) or _NECESSITY.search(low) or _FUTURE_3P.search(low)
                or _FIRST_PERSON.search(low))


def source_assignments(text: str, participants: list[str], base: date) -> list[Assignment]:
    """Cümledeki görev atamalarını yan cümle bazında çıkarır (LLM'siz, deterministik)."""
    found: list[Assignment] = []
    for clause in split_clauses(text):
        reported = _REPORTED.search(tr_lower(clause))
        if reported:  # yalnızca aktarılan kısım değerlendirilir (tr_lower uzunluğu korur)
            clause = clause[reported.end():].strip(" ,:")
        low = tr_lower(clause)
        if not clause or not _has_assignment_language(low):
            continue
        draft = extract_action(clause, participants, base)
        item = Assignment(clause=clause, owner=draft.owner, candidates=list(draft.owner_candidates),
                          spoken=draft.owner_spoken, due=draft.due_date, due_text=draft.date_text,
                          description=_LEADING_PARTICLE.sub("", draft.description))
        if not item.owner and not item.candidates and _FIRST_PERSON.search(low):
            item.first_person = True
        if item.owner or item.candidates or item.first_person or _IMPERATIVE.search(low) or _NECESSITY.search(low):
            found.append(item)
    return found


def grounded_person(spoken: str, source: str, participants: list[str]):
    """Modelin yazdığı ismi yalnızca kaynakta geçen kelimeleriyle çözer. (ad|None, adaylar)."""
    spoken = strip_honorifics((spoken or "").strip())
    heard = [w for w in spoken.split() if phrase_in(strip_name_suffix(w), source)]
    if not heard:
        return None, []
    match = best_name_match(" ".join(heard), participants)
    if match.is_ambiguous:
        return None, list(match.candidates)
    return (match.name if match.found else None), []


def best_clause(text: str, assignments: list[Assignment]) -> Assignment | None:
    if not assignments:
        return None
    if len(assignments) == 1:
        return assignments[0]
    return max(assignments, key=lambda a: grounded_ratio(text, a.clause) + grounded_ratio(a.clause, text))


@dataclass
class OwnerDecision:
    owner: str
    note: str = ""
    removed: bool = False  # model bir isim verdi ama kabul edilmedi
    clause: Assignment | None = None


def resolve_owner(model_owner: str, candidate_text: str, evidence: list[tuple[str, str]],
                  participants: list[str], base: date) -> OwnerDecision:
    """Sorumluyu kaynak satırlardan belirler. `evidence`: [(konuşmacı, metin)]."""
    model_owner = (model_owner or "").strip()
    per_line = [(speaker, a) for speaker, text in evidence for a in source_assignments(text, participants, base)]
    clause = best_clause(candidate_text, [a for _, a in per_line])
    speaker = next((s for s, a in per_line if a is clause), "")
    source_text = " ".join(text for _, text in evidence)

    if clause is not None:
        if clause.candidates:
            return OwnerDecision("", f"sorumlu belirsiz ({', '.join(clause.candidates)}); netleştirin",
                                 removed=bool(model_owner), clause=clause)
        if clause.owner:
            note = ""
            if model_owner:
                named, _ = grounded_person(model_owner, source_text, participants)
                if named != clause.owner:
                    note = f"model '{model_owner}' dedi; kaynak cümlede sorumlu {clause.owner}"
            return OwnerDecision(clause.owner, note, clause=clause)
        if clause.first_person:
            if speaker in participants:
                return OwnerDecision(speaker, "konuşmacı kendisi üstlendi", clause=clause)
            return OwnerDecision("", "üstlenen konuşmacı katılımcı listesinde yok", removed=bool(model_owner),
                                 clause=clause)
        if clause.spoken:  # liste dışı ad ("Raporu Burak hazırlasın")
            return OwnerDecision("", f"'{clause.spoken}' katılımcı listesinde yok", removed=bool(model_owner),
                                 clause=clause)
    if not model_owner:
        return OwnerDecision("", "", clause=clause)
    named, candidates = grounded_person(model_owner, source_text, participants)
    if candidates:
        return OwnerDecision("", f"sorumlu belirsiz ({', '.join(candidates)}); netleştirin", removed=True,
                             clause=clause)
    if named:
        return OwnerDecision(named, "", clause=clause)
    # Konuşmacının adı cümlede geçmiyor ve birinci şahıs taahhüdü yok: konuşmacı sorumlu yapılmaz
    return OwnerDecision("", f"sorumlu '{model_owner}' kaynak cümlede doğrulanamadı", removed=True, clause=clause)
