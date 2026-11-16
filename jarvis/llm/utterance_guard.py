"""Jarvis'e hitap edilen doğal cümleler için deterministik ön/son denetim (Faz 5c.1).

Mevcut LLM yönlendiricisinin (router.py) içinde çalışır; ayrı bir yönlendirme sistemi değildir.

  * `pre_llm_guard`: modele SORULMADAN önce. Olumsuzlanan istek ("bu kararı henüz kaydetme"), aynı cümlede
    birden fazla işlem ve basit selamlaşma/teşekkür burada yakalanır. İşlem yapılmaz, model çağrılmaz.
  * `post_llm_guard`: model KAYIT DEĞİŞTİREN bir niyet önerdiğinde. Cümle bir soru, değerlendirme
    ("vermeli miyiz?") veya varsayımsa ("hazırlarsa") öneri yapılmaz. Yalnızca okuma komutları (gündemi oku vb.)
    soru biçiminde de çalışır. Kibar istek ("ekler misin", "verebilir misin") soru sayılmaz; onaylı yoldan devam eder.

Kurallar bilinçli olarak dar tutuldu: yanlış "olumsuz" kararı yalnızca işlem YAPILMAMASINA yol açar
(kullanıcı açık komutla tekrar söyler); yanlış "olumlu" ise kayıt riskidir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..utils.turkish_text import tr_lower

# Komut fiilleri (kök). Olumsuz emir: kök + me/ma (+ yin/yın/yelim/yalım/sın/sin)
_VERBS = (r"kaydet|ekle|yaz|ver|başlat|bitir|gönder|yolla|sil|kapat|al|aç|ata|oluştur|işle|geç|ertele|değiştir"
          r"|kaldır|tamamla|işaretle|onayla|kayded|not et|not düş|kayda geç")
_NEG_IMPERATIVE = re.compile(
    rf"\b(?:{_VERBS})(?:me|ma)(?:yin|yın|yiniz|yınız|yelim|yalım|sın|sin|yiver|yıver)?"
    r"(?=\s*(?:$|[.,!;?]|lütfen\b|şimdilik\b|henüz\b|daha\b|sakın\b))")
_NEG_LEXICAL = re.compile(
    r"\b(?:istemiyorum|istemiyoruz|gerek yok|gerekmez|gerekmiyor|yapmayalım|yapma|yapmayın|sakın|dokunma)\b"
    r"|\bşimdilik (?:\w+ )?(?:kalsın|değil|bekle\w*)\b|\bvazgeçtik\b")

# Çoklu işlem: bağlaçla ayrılmış yan cümlelerin ikisinde de emir/istek fiili varsa
_CLAUSE_SPLIT = re.compile(r"\s*(?:,|;|\bve sonra\b|\bsonra\b|\bardından\b|\bayrıca\b|\bbir de\b|\bve\b)\s*")
_REQUEST_VERB = re.compile(
    rf"\b(?:{_VERBS}|oku|söyle|göster|listele|ekleyelim|verelim)(?:elim|alım|in|ın|iniz|ınız|sin|sın|ir misin|ır mısın"
    r"|er misin|ar mısın|abilir misin|ebilir misin)?$")

_CHAT = {
    "teşekkür": "Rica ederim.", "sağ ol": "Rica ederim.", "sağol": "Rica ederim.", "eline sağlık": "Rica ederim.",
    "merhaba": "Merhaba.", "selam": "Merhaba.", "günaydın": "Günaydın.", "iyi günler": "İyi günler.",
    "iyi akşamlar": "İyi akşamlar.", "nasılsın": "İyiyim, teşekkürler. Toplantıya yardımcı olmaya hazırım.",
    "naber": "İyiyim, teşekkürler. Toplantıya yardımcı olmaya hazırım.", "aferin": "Teşekkür ederim.",
    "harikasın": "Teşekkür ederim.", "çok iyi": "Teşekkür ederim.", "süper": "Teşekkür ederim.",
}
_CHAT_PATTERN = re.compile(
    r"^(?:" + "|".join(re.escape(k) for k in sorted(_CHAT, key=len, reverse=True)) + r")"
    r"(?:ler|lar|un|unuz|sun|\s?ederim|\s?ederiz)?(?:\s+(?:jarvis|dostum|çok|canım))?\s*[.!?]*$")
CHAT_SUFFIX = " Genel sohbet henüz desteklenmiyor; toplantı komutları için «Jarvis, yardım» diyebilirsiniz."

_QUESTION_PARTICLE = re.compile(r"\?|\bm[ıiuü](?:yım|yim|yum|yüm|sın|sin|sun|sün|yız|yiz|yuz|yüz|dır|dir|dur|dür)?\b"
                                r"|\b(?:ne|neden|niçin|niye|nasıl|kim|kime|kimi|hangi|kaç|nerede|nereye)\b")
_POLITE = re.compile(r"\b\w+(?:abilir|ebilir)\s*m[ıi]\w*\b|\b\w+(?:ar|er|ır|ir|ur|ür)\s+m[ıiuü]s[ıiuü]n\w*\b"
                     r"|\blütfen\b|\b\w+(?:sana|sene)\b"
                     # "-elim mi" bir öneridir ("geçelim mi?" = geçelim); onaylı yoldan gider (R49)
                     r"|\b\w+(?:elim|alım)\s+m[ıi]\b")
_DELIBERATIVE = re.compile(r"(?:malı|meli)\s*m[ıi]\w*\b|\bacaba\b|\bsence\w*\b|\bne dersin\w*\b"
                           r"|\b(?:gerek(?:ir|iyor)?|lazım)\s+m[ıi]\b|\byoksa\b|\bm[ıi] ki\b|\bdoğru mu\b")
_HYPOTHETICAL = re.compile(r"\beğer\b|\bdiyelim ki\b|\bvarsayalım\b|\bfarz edelim\b|\bolursa\b"
                           r"|\b\w+(?:ırsa|irse|ursa|ürse|arsa|erse|acaksa|ecekse|mazsa|mezse)\b")


@dataclass
class GuardResult:
    category: str  # negated | multi | chat | question | hypothetical | policy | injection | incomplete
    message: str
    kind: str = "no_action"  # policy/injection: "blocked" (güvenlik reddi)


NEGATED_MESSAGE = "Anlaşıldı, bu isteği uygulamıyorum; hiçbir işlem yapılmadı."
MULTI_MESSAGE = ("Bu cümlede birden fazla işlem var; güvenlik için hiçbiri yapılmadı. Lütfen her birini ayrı "
                 "söyleyin.")
QUESTION_MESSAGE = ("Bu bir soru gibi görünüyor; işlem yapmadım. Kayıt için açık komut kullanın "
                    "(ör. «Jarvis, görev: Mehmet raporu cumaya kadar hazırlasın»).")
DELIBERATIVE_MESSAGE = ("Bu bir değerlendirme sorusu gibi; kararı siz verirsiniz, işlem yapmadım. Karar verdiğinizde "
                        "açık komutla söyleyin (ör. «Jarvis, görev: ...»).")
HYPOTHETICAL_MESSAGE = ("Bu bir varsayım veya koşul gibi görünüyor; işlem yapmadım. Gerekirse kesin komutla "
                        "tekrar söyleyin.")


def _low(text: str) -> str:
    return " ".join(tr_lower(text or "").replace("’", "'").split())


def is_negated(command: str) -> bool:
    low = _low(command)
    return bool(_NEG_IMPERATIVE.search(low) or _NEG_LEXICAL.search(low))


# Önceki yan cümleyi kaydetme talimatı ayrı bir işlem değildir: "Selim raporu bitirsin, görev olarak ekle"
_META_CLAUSE = re.compile(r"^(?:bunu |onu |şunu |(?:bu )?(?:görevi|kararı|notu|aksiyonu) )?"
                          r"(?:(?:karar|görev|not|aksiyon) olarak )?(?:sisteme |listeye |kayda )?"
                          r"(?:ekle|yaz|kaydet|not al|not et|kayda geç)\w*$")


def count_requests(command: str) -> int:
    """Bağlaçlarla ayrılmış yan cümlelerden kaçı ayrı bir istek fiiliyle bitiyor."""
    clauses = [c for c in _CLAUSE_SPLIT.split(_low(command).strip(" .!?")) if c]
    return sum(1 for clause in clauses if _REQUEST_VERB.search(clause) and not _META_CLAUSE.match(clause))


def chat_reply(command: str) -> str | None:
    low = _low(command).strip(" ,")
    if not _CHAT_PATTERN.match(low):
        return None
    key = next(k for k in sorted(_CHAT, key=len, reverse=True) if low.startswith(k))
    return _CHAT[key]


def is_question(command: str) -> bool:
    return bool(_QUESTION_PARTICLE.search(_low(command)))


def is_deliberative(command: str) -> bool:
    """"vermeli miyiz?", "sence", "acaba", "yoksa": karar değerlendirme sorusu (işlem isteği değil)."""
    return bool(_DELIBERATIVE.search(_low(command)))


def is_polite_request(command: str) -> bool:
    return bool(_POLITE.search(_low(command)))


# Onay/güvenlik kurallarını değiştirme isteği (R64): ret, sohbet değil
_POLICY_CHANGE = re.compile(
    r"\bonay\w*\s+(?:sormadan|istemeden|almadan|beklemeden|olmadan)\b"
    r"|\bonay\w*\s+(?:\w+\s+)?(?:kapat|kaldır|atla|devre dışı|isteme|sorma)\w*"
    r"|\b(?:sormadan|onaysız|izinsiz|teyitsiz)\s+(?:\w+\s+)?(?:kaydet|kayded|ekle|yaz|uygula|gönder|sil)\w*"
    r"|\b(?:güvenlik\w*|kısıtlama\w*|onay mekanizma\w*)\s+(?:\w+\s+)?(?:kapat|kaldır|devre dışı|yok say|unut|esnet|"
    r"değiştir|gevşet|atla)\w*"
    r"|\b(?:kuralları|kurallarını)\s+(?:unut|yok say|esnet|gevşet|devre dışı)\w*")
# Komut içinde JSON / niyet adı / sistem talimatı (HR24): metin veridir, uygulanmaz
_EMBEDDED_INSTRUCTION = re.compile(
    r"\b(?:json\w*|intent|arguments|sistem (?:talimat|istem|prompt)\w*|prompt\w*|talimatları?\s+(?:unut|yok say))\b")
_INTENT_NAME = re.compile(r"\b[A-Z]{3,}(?:_[A-Z]+)+\b")
POLICY_MESSAGE = ("Onay ve güvenlik kuralları sesli komutla değiştirilemez; kayıt değiştiren her öneri için «Jarvis, "
                  "evet» onayı istenmeye devam edecek. Hiçbir işlem yapılmadı.")
INJECTION_MESSAGE = ("Komut içindeki JSON, niyet adı veya talimat doğrudan uygulanmaz; her işlem normal komut ve onay "
                     "kurallarıyla yapılır. Hiçbir işlem yapılmadı.")


def security_guard(command: str) -> GuardResult | None:
    """Güvenlik zincirini değiştirme veya atlama girişimleri: işlem yok, açık ret mesajı."""
    low = _low(command)
    if _POLICY_CHANGE.search(low):
        return GuardResult("policy", POLICY_MESSAGE, "blocked")
    if _EMBEDDED_INSTRUCTION.search(low) or _INTENT_NAME.search(command or ""):
        message = INJECTION_MESSAGE
        from .router import EXPLICIT_ONLY_INTENTS  # noqa: PLC0415 (döngüsel içe aktarmayı önlemek için)

        named = [i for i in EXPLICIT_ONLY_INTENTS if i.value in (command or "")]
        if named:
            example = EXPLICIT_ONLY_INTENTS[named[0]]
            message += f" Bu önemli bir işlem; yalnızca açık komutla yapılır: «Jarvis, {example}»."
        return GuardResult("injection", message, "blocked")
    return None


# Yarım kalmış ek: tamamlanmış bir yan cümleden sonra yüklemsiz, "de/da/ve/ile" ile biten kısa parça (D33:
# "Kerem ve Nihat geldi, Selin de"). İlk kısmı işleyip kalanı atlamak kısmi kayda yol açar; tamamı sorulur.
_FRAGMENT_SPLIT = re.compile(r"\s*[,;]\s*")
_FRAGMENT_END = re.compile(r"(?:^|\s)(?:de|da|ve|ile|bir de|ayrıca)$")
_HAS_PREDICATE = re.compile(r"\b\w+(?:dı|di|du|dü|tı|ti|tu|tü)(?:m|n|k|nız|niz|nuz|nüz|lar|ler)?\b"
                            r"|\b\w+(?:mış|miş|muş|müş|ıyor|iyor|uyor|üyor|acak|ecek|sın|sin|sun|sün|elim|alım)\w*\b"
                            r"|\b(?:var|yok|burada|aramızda)\b")


def dangling_fragment(command: str) -> str | None:
    """Son yan cümle yarım kalmışsa onu döndürür; değilse None."""
    clauses = [c for c in _FRAGMENT_SPLIT.split(_low(command).strip(" .!?")) if c]
    if len(clauses) < 2:
        return None
    last = clauses[-1]
    if len(last.split()) <= 4 and _FRAGMENT_END.search(last) and not _HAS_PREDICATE.search(last) \
            and any(_HAS_PREDICATE.search(c) for c in clauses[:-1]):
        return last
    return None


def pre_llm_guard(command: str) -> GuardResult | None:
    security = security_guard(command)
    if security is not None:
        return security
    if is_negated(command):
        return GuardResult("negated", NEGATED_MESSAGE)
    fragment = dangling_fragment(command)
    if fragment is not None:
        return GuardResult("incomplete", f"Cümle yarım kaldı gibi: «{fragment}» için ne demek istediniz? Hiçbir "
                                         "kayıt yapılmadı. Lütfen tamamını tek cümlede söyleyin (ör. «Jarvis, Ad, "
                                         "Ad ve Ad geldi»).", "clarify")
    if count_requests(command) >= 2:
        return GuardResult("multi", MULTI_MESSAGE)
    reply = chat_reply(command)
    if reply is not None:
        return GuardResult("chat", reply + CHAT_SUFFIX)
    return None


def post_llm_guard(command: str) -> GuardResult | None:
    """Model kayıt değiştiren bir niyet önerdiğinde çağrılır."""
    low = _low(command)
    if _HYPOTHETICAL.search(low):
        return GuardResult("hypothetical", HYPOTHETICAL_MESSAGE)
    if _DELIBERATIVE.search(low):
        return GuardResult("question", DELIBERATIVE_MESSAGE)
    if is_question(command) and not is_polite_request(command):
        return GuardResult("question", QUESTION_MESSAGE)
    return None
