"""Çıkarılan adayların bağlama göre deterministik düzeltilmesi (Faz 5a.1).

LLM (veya kural tabanlı çıkarıcı) her satırı tek tek değerlendirir; toplantıda ise anlam çoğu zaman
satırlar arasındadır: bir öneri sonraki satırda reddedilir, bir tarih pazarlıkla değişir, bir soru
karar gibi görünür. Bu modül, model çıktısından SONRA, ek LLM çağrısı yapmadan şu kuralları uygular:

  1. Talimat gibi görünen satırlar (sahte "sistem notu", "kuralları unut", "Jarvis, ... sil") güvenilmez
     veridir: yalnızca bu satırlara dayanan aday atılır.
  2. Yalnızca soru cümlelerine dayanan karar/görev → soru.
  3. Karar dili ("karar verdik", "onaylandı", "uygulanacak") olmayan karar → öneri; aynı bölümde
     itiraz/belirsizlik varsa → açık konu.
  4. Sonradan itiraz edilen, ertelenen veya geri alınan karar → açık konu (sıra korunur).
  5. Atama dili olmayan görev (kişiye verilmemiş, emir/gelecek/gereklilik yok) → öneri veya atılır.
  6. "Ben yapacağım" diyen konuşmacı sorumlu olur. Tarih kaynak cümleden ve izleyen pazarlıktan
     çözülür; çelişki çözülmemişse tarih boş bırakılır.
  7. Aynı satırdan tekrar eden adaylar birleştirilir.
  8. Çözülmemiş konu işaretleri ("netleştiremedik", "sonra konuşuruz") için açık konu eklenir.

Kurallar kasıtlı olarak temkinlidir: belirsizlikte kayıt üretmek yerine daha zayıf bir türe
(öneri / açık konu) düşer ya da alanı boş bırakır. Hepsi taslaktır; insan onayı değişmez.
"""
from __future__ import annotations

import re
from datetime import date

from ..meeting.models import DraftKind
from ..utils.dates import DateMatch, find_date, remove_span
from ..utils.turkish_text import tr_lower
from .attribution import resolve_owner, source_assignments
from .grounding import _word_matches, content_words, grounded_ratio

_INSTRUCTION_TOPIC = re.compile(r"\b(?:sistem (?:notu|mesajı|talimatı|komutu)|kural\w*|talimat\w*|yönerge\w*|prompt\w*"
                                r"|yapay zek\w*|json)\b")
_INSTRUCTION_VERB = re.compile(r"\b(?:unut|yok say|görmezden gel|yap|yaz|uygula|sil|gönder|işaretle|ata)\b")
_DICTATION = re.compile(r"\b(?:karar|görev)\w* (?:olarak )?yaz\b|\b(?:özet\w*|tutana\w*) (?:sonuna|başına)\b")
# "Jarvis'in raporu" gibi ekli kullanım hitap değildir (kurallı ayrıştırıcıyla aynı kural)
_WAKE = re.compile(r"(?:^|[\s,.;:!?\"])jarvis(?:$|[\s,.;:!?\"])")

_DECISION = re.compile(
    r"\bkarar(?:ı|ını)? (?:verdik|verildi|aldık|alındı)\b|\bkararlaştır\w*|\bkesinleş(?:ti|tirdik|tirildi)\b"
    r"|\bonayl(?:andı|adık|adı|ıyoruz|ıyorum|anmıştır)\b|\banlaştık\b|\bmutabık\w*|\bkarara bağla\w*"
    r"|\bkabul (?:edildi|ettik|ediyoruz)\b"
    r"|\bbelirlendi\b|\bnetleş(?:ti|tirdik|tirildi)\b"
    r"|\b\w+(?:ılacak|ilecek|ulacak|ülecek|ınacak|inecek|unacak|ünecek|nacak|necek)\b")
_SUGGESTION_WORDS = ("öneri", "önerim", "bence", "belki", "olabilir", "yapsak", "ne dersiniz", "düşünebiliriz",
                     "teklif ediyorum", "derim", "edelim mi", "yapalım mı", "alalım mı", "seçelim mi", "istersek")
_HORTATIVE = re.compile(r"^[a-zçğıöşü]{2,}(?:alım|elim|yalım|yelim)$")  # uygulayalım, erteleyelim
_UNRESOLVED = re.compile(r"\bnetleştireme\w*|\bbelli değil\b|\bkarar vermeyelim\b"
                         r"|\bsonra (?:konuşuruz|bakarız|görüşürüz)\b"
                         r"|\b(?:gelecek|sonraki|bir sonraki|önümüzdeki) toplantı(?:da|ya)\b.*"
                         r"\b(?:netleştir|konuş|görüş|ele al|karar ver|bak)\w*"
                         r"|\btekrar ele alalım\b|\banlaşama\w*|\bçözülme\w*|\baskıda\b|\bbelirsiz\b")
_DISAGREE = re.compile(r"\b(?:olmaz|uymaz|katılmıyorum|itirazım var|yetişmez|mümkün değil|kabul etmiyorum)\b")
# "ertele-" fiili çekimde "ertel-iyoruz" olur (e→i); kök "ertel" ile aranır.
_REVERSAL = re.compile(r"\b(?:iptal (?:ediyoruz|ettik|edildi)|vazgeç\w*|geri al(?:ıyoruz|dık|ındı)|düzeltiyorum"
                       r"|ertel\w*|askıya al\w*)\b")
_AGREE = re.compile(r"^(?:tamam|peki|olur|anlaştık|uygun)\b|\b(?:olsun|uygundur|kabul)\b")
_IMPERATIVE = re.compile(r"\b\w{2,}(?:sın|sin|sun|sün|sınlar|sinler|sunlar|sünler)\b")
_FUTURE = re.compile(r"\b\w+(?:acak|ecek|acağım|eceğim|acağız|eceğiz|acaklar|ecekler)\b")
_FIRST_PERSON = re.compile(r"\bben\b|\b\w+(?:acağım|eceğim|ayım|eyim)\b")
_NECESSITY = re.compile(r"\b\w+(?:malı|meli|malıyız|meliyiz)\b|\b(?:lazım|gerek|gerekiyor)\b|\bgörev\w*|\bsorumlu\w*")
_QUESTION_PARTICLE = re.compile(r"\b(?:mı|mi|mu|mü)(?:sın|sin|sunuz|sünüz|yız|yiz|yuz|yüz|dır|dir)?\b")


def _low(text: str) -> str:
    return tr_lower(" ".join((text or "").split()))


def is_instruction_like(text: str) -> bool:
    """Toplantı konuşması değil, asistana/modele talimat gibi görünen satır."""
    low = _low(text)
    if _WAKE.search(low):
        return True
    if _DICTATION.search(low):
        return True
    return bool(_INSTRUCTION_TOPIC.search(low) and _INSTRUCTION_VERB.search(low))


def is_question(text: str) -> bool:
    low = _low(text)
    return low.endswith("?") or bool(_QUESTION_PARTICLE.search(low))


def has_decision_language(text: str) -> bool:
    return bool(_DECISION.search(_low(text)))


def has_suggestion_language(text: str) -> bool:
    low = _low(text)
    if any(word in low for word in _SUGGESTION_WORDS):
        return True
    # Büyük harfle başlayan kelimeler (Selim gibi adlar) emir/öneri eki sayılmaz
    return any(_HORTATIVE.match(tr_lower(w.strip(".,;:!?"))) for w in (text or "").split()
               if w[:1].islower())


def has_assignment_signal(text: str, owner_known: bool) -> bool:
    low = _low(text)
    if _IMPERATIVE.search(low) or _NECESSITY.search(low):
        return True
    return owner_known and bool(_FUTURE.search(low))


def _shares_topic(a: str, b: str) -> bool:
    words_a, words_b = content_words(a), content_words(b)
    return any(_word_matches(x, y) for x in words_a for y in words_b)


def _all_dates(text: str, base: date) -> list[DateMatch]:
    found: list[DateMatch] = []
    working = text
    for _ in range(4):
        match = find_date(working, base)
        if match is None:
            break
        found.append(match)
        working = remove_span(working, match.start, match.end)
    return found


def _index(line_id: str) -> int:
    return int(line_id[1:])


def refine_candidates(candidates: list, lines: list, participants: list[str], meeting_date: date,
                      stats=None, dropped: list | None = None) -> list:
    """Adayları bölüm (chunk) bağlamında düzeltir. `lines`: o bölümün Line nesneleri (sıralı)."""
    by_id = {line.id: line for line in lines}
    ordered = [line.id for line in lines]
    chunk_text = " ".join(line.entry.text for line in lines)
    # Geri alma/erteleme ("kararı erteliyoruz") da bölümü belirsiz kılar (HE10 bulgusu)
    chunk_unsettled = bool(_UNRESOLVED.search(_low(chunk_text)) or _DISAGREE.search(_low(chunk_text))
                           or _REVERSAL.search(_low(chunk_text)))
    result = []
    for c in candidates:
        evidence = [e for e in c.evidence if e in by_id]
        if not evidence:
            continue
        ev_text = " ".join(by_id[e].entry.text for e in evidence)

        # 1) Talimat gibi görünen içerik (prompt enjeksiyonu): bu satırlara dayanan aday atılır.
        if all(is_instruction_like(by_id[e].entry.text) for e in evidence):
            if stats is not None:
                stats.rejected_injection += 1
            if dropped is not None:
                dropped.append({"text": c.text, "kind": c.kind, "neden": "talimat benzeri satıra dayanıyor"})
            continue

        # 2) Soru, karar veya görev değildir.
        if c.kind in (DraftKind.DECISION, DraftKind.ACTION) and all(is_question(by_id[e].entry.text)
                                                                    for e in evidence):
            c.kind = DraftKind.QUESTION
            _restore_text(c)
            c.owner, c.due_date, c.due_text = "", None, ""
            c.notes = [n for n in c.notes if "belirtilmedi" not in n] + ["soru cümlesi; karar/görev değil"]
            _count(stats, "downgraded")

        # 3) Karar dili yoksa karar değildir.
        if c.kind == DraftKind.DECISION and not has_decision_language(ev_text):
            c.kind = DraftKind.OPEN_ISSUE if chunk_unsettled else DraftKind.SUGGESTION
            c.notes.append("kesin karar ifadesi yok; " + ("itiraz/belirsizlik var, açık konu" if chunk_unsettled
                                                         else "öneri olarak işaretlendi"))
            _count(stats, "downgraded")

        # 4) Sonradan itiraz/erteleme/geri alma → açık konu
        if c.kind == DraftKind.DECISION:
            last = max(_index(e) for e in evidence)
            for later in ordered:
                if _index(later) <= last:
                    continue
                text = by_id[later].entry.text
                low = _low(text)
                if (_UNRESOLVED.search(low) or _DISAGREE.search(low) or _REVERSAL.search(low)) and \
                        (_shares_topic(text, ev_text) or _REVERSAL.search(low)):
                    c.kind = DraftKind.OPEN_ISSUE
                    c.evidence = evidence + [later]
                    c.notes.append(f"{later} satırında itiraz/erteleme var; kesin karar sayılmadı")
                    _count(stats, "downgraded")
                    break

        # 4b) Geri alınan/ertelenen bir karara dayanan öneri veya karar → açık konu
        if c.kind in (DraftKind.DECISION, DraftKind.SUGGESTION):
            reversal = next((e for e in evidence if _REVERSAL.search(_low(by_id[e].entry.text))), None)
            if reversal is not None:
                earlier = _earlier_decision(reversal, ordered, by_id)
                if earlier is not None:
                    c.kind = DraftKind.OPEN_ISSUE
                    c.evidence = sorted(set(evidence) | {earlier}, key=_index)
                    c.notes.append(f"{earlier} satırındaki karar {reversal} satırında geri alındı/ertelendi")
                    _count(stats, "downgraded")

        if c.kind == DraftKind.ACTION:
            # 6a) Sorumlu kaynak cümleden belirlenir; konuşmacı yalnızca "ben ... yapacağım" diyorsa sorumludur
            decision = resolve_owner(c.owner, c.text, [(by_id[e].entry.speaker, by_id[e].entry.text)
                                                       for e in evidence], participants, meeting_date)
            if decision.removed:
                _count(stats, "removed_fields")
            c.owner = decision.owner
            c.notes = [n for n in c.notes if n != "sorumlu belirtilmedi" and not n.startswith("sorumlu ")]
            if decision.note:
                c.notes.append(decision.note)
            elif not c.owner:
                c.notes.append("sorumlu belirtilmedi")
            clause = decision.clause
            if clause is not None and c.owner and clause.description and \
                    grounded_ratio(clause.description, c.text) >= 0.5:
                c.text = clause.description  # sorumlu adı ve tarih metinden ayrılır
            # 5) Atama dili yoksa görev değildir
            if not has_assignment_signal(ev_text, bool(c.owner)) and not _FIRST_PERSON.search(_low(ev_text)):
                if has_suggestion_language(ev_text):
                    c.kind = DraftKind.SUGGESTION
                    _restore_text(c)
                    c.owner, c.due_date, c.due_text = "", None, ""
                    c.notes = ["kişiye atanmış bir iş değil; öneri olarak işaretlendi"]
                    _count(stats, "downgraded")
                else:
                    _count(stats, "rejected_items")
                    if dropped is not None:
                        dropped.append({"text": c.text, "kind": c.kind,
                                        "neden": "kişiye atanmış iş değil (atama dili yok)"})
                    continue
            else:
                _resolve_due_date(c, evidence, ordered, by_id, meeting_date)
        result.append(c)
    result = _dedupe(result)
    result += _split_clause_supplement(result, lines, by_id, participants, meeting_date)
    result += _unresolved_supplement(result, lines)
    result += _reversal_supplement(result, ordered, by_id)
    result += _uncovered_supplement(result, lines)
    return result


def _earlier_decision(reversal_id: str, ordered: list[str], by_id: dict) -> str | None:
    text = by_id[reversal_id].entry.text
    for line_id in reversed([o for o in ordered if _index(o) < _index(reversal_id)]):
        earlier = by_id[line_id].entry.text
        if has_decision_language(earlier) and _shares_topic(earlier, text):
            return line_id
    return None


def _new_candidate(kind: str, text: str, evidence: list[str], note: str, **fields):
    from .analyzer import Candidate  # noqa: PLC0415

    return Candidate(kind, text, evidence, source="kural", notes=[note], **fields)


def _split_clause_supplement(candidates: list, lines: list, by_id: dict, participants: list[str],
                             meeting_date: date) -> list:
    """Aynı cümlede birden çok kişiye iş verilmişse eksik kalan görevleri ekler (E16, HE14)."""
    added = []
    action_lines = {e for c in candidates if c.kind == DraftKind.ACTION for e in c.evidence}
    for line_id in sorted(action_lines, key=_index):
        line = by_id.get(line_id)
        if line is None or is_instruction_like(line.entry.text):
            continue
        assignments = source_assignments(line.entry.text, participants, meeting_date)
        if len(assignments) < 2:
            continue
        on_line = [c for c in candidates + added if c.kind == DraftKind.ACTION and line_id in c.evidence]
        for a in assignments:
            owner = a.owner or (line.entry.speaker if a.first_person and line.entry.speaker in participants else "")
            covered = any((owner and c.owner == owner) or (not owner and grounded_ratio(a.clause, c.text) >= 0.6)
                          for c in on_line)
            if covered or not (owner or a.candidates):
                continue
            note = (f"sorumlu belirsiz ({', '.join(a.candidates)}); netleştirin" if a.candidates
                    else "aynı cümledeki ayrı görev (kural)")
            added.append(_new_candidate(DraftKind.ACTION, a.description or a.clause, [line_id], note, owner=owner,
                                        due_date=a.due, due_text=a.due_text))
    return added


def _reversal_supplement(candidates: list, ordered: list[str], by_id: dict) -> list:
    """Geri alınan karar için açık konu yoksa ekler (sıra korunur)."""
    if any(c.kind == DraftKind.OPEN_ISSUE for c in candidates):
        return []
    for line_id in ordered:
        text = by_id[line_id].entry.text
        if _REVERSAL.search(_low(text)) and not is_instruction_like(text):
            earlier = _earlier_decision(line_id, ordered, by_id)
            if earlier is not None:
                return [_new_candidate(DraftKind.OPEN_ISSUE, text.strip(" ."), [earlier, line_id],
                                       f"{earlier} satırındaki karar geri alındı/ertelendi (kural)")]
    return []


_PHATIC = re.compile(r"\b(?:duyuyor|görüyor|hazır|tamam|anlaşıldı|başka|var)\s+m[ıiuü]\w*\b")


def _uncovered_supplement(candidates: list, lines: list) -> list:
    """Modelin atladığı açık soru ve öneri satırlarını kural tabanlı ekler (HE05, E25). Yalnızca taslaktır."""
    covered = {e for c in candidates for e in c.evidence}
    added = []
    for line in lines:
        text = line.entry.text.strip()
        if line.id in covered or is_instruction_like(text) or len(content_words(text)) < 3:
            continue
        if is_question(text) and not _PHATIC.search(_low(text)):
            added.append(_new_candidate(DraftKind.QUESTION, text.strip(" ."), [line.id], "açık soru (kural)"))
        elif has_suggestion_language(text) and not has_decision_language(text):
            added.append(_new_candidate(DraftKind.SUGGESTION, text.strip(" ."), [line.id], "öneri (kural)"))
    return added


def _restore_text(c) -> None:
    """Görev için ayıklanan tarih ifadesi, aday görev olmaktan çıkınca metne geri döner."""
    if getattr(c, "original_text", ""):
        c.text, c.original_text = c.original_text, ""


def _count(stats, name: str) -> None:
    if stats is not None:
        setattr(stats, name, getattr(stats, name, 0) + 1)


def _resolve_due_date(c, evidence: list[str], ordered: list[str], by_id: dict, meeting_date: date) -> None:
    """Tarihi kaynak cümleden ve hemen ardından gelen tarih pazarlığından çözer (sıra korunur)."""
    mentions: list[tuple[str, DateMatch]] = []
    for e in evidence:
        mentions += [(e, m) for m in _all_dates(by_id[e].entry.text, meeting_date)]
    last = max(_index(e) for e in evidence)
    owner_speaker = c.owner
    for later in ordered:
        if not 0 < _index(later) - last <= 3:
            continue
        line = by_id[later]
        dates = _all_dates(line.entry.text, meeting_date)
        low = _low(line.entry.text)
        if dates and (line.entry.speaker == owner_speaker or _AGREE.search(low) or _DISAGREE.search(low)):
            mentions += [(later, m) for m in dates]
            if later not in c.evidence:
                c.evidence.append(later)
    distinct = {m.value for _, m in mentions}
    if not distinct:
        return  # cümlede tarih yok: model ne derse desin tarih boş kalır (validate_item zaten doğruladı)
    if len(distinct) == 1:
        value = next(iter(distinct))
        if c.due_date != value:
            c.due_date, c.due_text = value, mentions[0][1].text
        return
    final_line, final = mentions[-1]
    if _AGREE.search(_low(by_id[final_line].entry.text)):
        c.due_date, c.due_text = final.value, final.text
        c.notes.append(f"tarih görüşmede değişti; {final_line} satırındaki son mutabakat alındı")
    else:
        c.due_date, c.due_text = None, ""
        c.notes = [n for n in c.notes if n != "son tarih belirtilmedi"]
        c.notes.append("tarih üzerinde anlaşma yok; netleştirin")


def _dedupe(candidates: list) -> list:
    kept: list = []
    for c in candidates:
        duplicate = False
        for k in kept:
            same_lines = set(c.evidence) & set(k.evidence)
            if not same_lines:
                continue
            similar = grounded_ratio(c.text, k.text) >= 0.6 or grounded_ratio(k.text, c.text) >= 0.6
            if c.kind == k.kind and c.owner == k.owner and similar:
                duplicate = True
            # Aynı cümleden hem karar hem sorumlusuz görev çıkarılmışsa görev tekrardır
            if c.kind == DraftKind.ACTION and k.kind == DraftKind.DECISION and not c.owner and similar:
                duplicate = True
            if duplicate:
                break
        if not duplicate:
            kept.append(c)
    return kept


def _unresolved_supplement(candidates: list, lines: list) -> list:
    """Bölümde çözülmemiş konu işareti varsa ve henüz açık konu çıkarılmadıysa bir açık konu ekler."""
    from .analyzer import Candidate  # noqa: PLC0415

    if any(c.kind == DraftKind.OPEN_ISSUE for c in candidates):
        return []
    has_decision = any(has_decision_language(line.entry.text) for line in lines)
    for pattern, needs_no_decision in ((_UNRESOLVED, False), (_DISAGREE, True)):
        if needs_no_decision and has_decision:
            continue
        for position, line in enumerate(lines):
            text = line.entry.text
            if not pattern.search(_low(text)) or is_instruction_like(text):
                continue
            # İtiraz sonradan "tamam, ... olsun" ile çözüldüyse açık konu değildir
            if needs_no_decision and any(_AGREE.search(_low(later.entry.text)) for later in lines[position + 1:]):
                continue
            previous = [p for p in lines if _index(p.id) < _index(line.id)][-2:]
            evidence = [p.id for p in previous] + [line.id]
            return [Candidate(DraftKind.OPEN_ISSUE, text.strip(" ."), evidence, source="kural",
                              notes=["çözülmemiş konu işareti (kural)"])]
    return []
