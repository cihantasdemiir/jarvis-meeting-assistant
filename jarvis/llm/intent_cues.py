"""Modelden bağımsız Türkçe niyet ipuçları (Faz 5c.1 düzeltmesi).

Gerçek qwen3.5:4b ölçümünde (doğal set 32/36) görülen dört hata sınıfı için kullanılır:

  * Model kararsız kaldı (NONE, düşük güven) ama cümlede açık bir gündem isteği var (D09, D10).
  * Model salt okunur bir niyet seçti ama kullanıcı kayıt istiyor (D14: "yoklamaya ekler misin" → ATTENDANCE).
    Okuma niyetiyle bir yazma isteği sessizce yok sayılamaz.
  * Bağlam + istek içeren birleşik cümlede model bağlam kısmını seçti (D35: "X'in konuşması bitti, sıradaki
    maddeye geçebiliriz" → RELEASE_SPEAKER).

Bu ipuçları HİÇBİR ŞEYİ ÇALIŞTIRMAZ. Yalnızca hangi izinli niyetin önerileceğini belirler; öneri ardından mevcut
zincirden geçer (son denetim, `ground_intent`, kanonik komut, kurallı ayrıştırıcıda yeniden doğrulama, kayıt
değiştiren her işlem için «Jarvis, evet»). Kalıplar sözcük sınıflarına (gündem adları, okuma ve ekleme fiilleri,
istek ekleri) dayanır; değerlendirme setindeki cümleler buraya yazılmamıştır.
"""
from __future__ import annotations

import re

from ..utils.turkish_text import tr_lower
from ..voice.command_parser import IntentType

_AGENDA_NOUN = r"(?:gündem\w*|başlı(?:k|ğ)\w*|madde\w*|konu(?!ş)\w*)"
# Yeni madde ekleme fiilleri ("olsun" yalnızca gündem/başlık adıyla birlikte anlamlıdır)
_ADD_VERB = r"(?:ekle\w*|dahil e(?:t|d)\w*|koy\w*|al(?:alım|ır mısın|abilir\w*|ın)|olsun|yaz\w*)"
_READ_VERB = (r"(?:oku\w*|say(?:ar|ın|abilir\w*|sana)|söyle\w*|hatırlat\w*|listele\w*|sırala\w*|göster\w*"
              r"|neler\w*|hangi\w*"
              r"|ne(?:ler)? var|nedir|bulunuyor\w*|var mı)")
_AGENDA_TALK = r"(?:görüşece\w*|konuşaca\w*|ele alaca\w*|ele alınaca\w*|görüşülece\w*)"
# Kayıt isteği fiilleri: okuma niyeti seçilse bile bunlar varsa işlem sessizce okumaya çevrilmez
_WRITE_REQUEST = re.compile(r"\b(?:ekle\w*|dahil e(?:t|d)\w*|kaydet\w*|kayded\w*|işaretle\w*|kayda geç\w*"
                            r"|yaz(?:ar m[ıi]s[ıi]n|abilir\w*|alım|ın|sana)?\b|not (?:al|et)\w*)")
_ATTENDANCE_NOUN = re.compile(r"\b(?:yoklama\w*|katılımcı\w*|listeye|aramıza|katılanlar\w*)\b|\b(?:de|da) geldi\w*\b")

_ADD_AGENDA = re.compile(rf"\b{_AGENDA_NOUN}\b.*\b{_ADD_VERB}\b")
_READ_AGENDA = re.compile(rf"\b{_AGENDA_NOUN}\b.*\b{_READ_VERB}\b|\b{_READ_VERB}\b.*\b{_AGENDA_NOUN}\b"
                          rf"|\b{_AGENDA_TALK}\b")
_NEXT_ITEM = re.compile(r"\b(?:sıradaki|sonraki|diğer|bir sonraki|öbür)\s+(?:madde|konu(?!ş)|başlı)\w*"
                        r"\s+(?:\w+\s+)?geç\w*")
_RELEASE = re.compile(r"\bsöz\w*\s+(?:\w+\s+)?(?:kapat|bitir|bırak|sonlandır)\w*"
                      r"|\bkonuşma\w*\s+(?:kapat|bitir|sonlandır)\w*")

# İstek biçimli yan cümle: emir, -elim/-alım, -ebiliriz, -ebilir misin, -er misin, -sın, olsun
_REQUEST_FORM = re.compile(
    r"\b\w+(?:elim|alım|ebiliriz|abiliriz|ebilir misin\w*|abilir misin\w*|ebilir miyiz|abilir miyiz|sana|sene)\b"
    r"|\b\w+(?:er|ar|ır|ir|ur|ür) m[ıiuü]s[ıiuü]n\w*\b|\b\w+(?:sın|sin|sun|sün)\b$|\bolsun\b"
    r"|\b(?:ekle|kaydet|oku|söyle|geç|kapat|bitir|bırak|ver|başlat|yaz|listele|göster)\b$")
_PREDICATE = re.compile(r"\b\w+(?:dı|di|du|dü|tı|ti|tu|tü)(?:m|n|k|nız|niz|nuz|nüz|lar|ler)?\b"
                        r"|\b\w+(?:mış|miş|muş|müş|ıyor|iyor|uyor|üyor|acak|ecek)\w*\b|\b(?:var|yok|tamam)\b")
_CLAUSES = re.compile(r"\s*(?:,|;|\bama\b|\bfakat\b|\bancak\b)\s*")

# Gündem başlığı: "gündeme X (başlığını) (da) ekle..." veya "son/sonraki madde (olarak) X olsun/ekle"
_TITLE_PATTERNS = [
    re.compile(rf"\bgündem(?:e|imize|imizin sonuna|in sonuna)\s+(?:de\s+|da\s+)?(?P<t>.+?)\s+"
               rf"(?:(?:başlığını|maddesini|konusunu|başlığı|maddesi|konusu)\s+)?(?:da\s+|de\s+)?{_ADD_VERB}"),
    re.compile(rf"\b(?:son|ilk|bir sonraki|sonraki|yeni)\s+(?:madde|başlı|konu(?!ş))\w*(?:\s+olarak)?\s+(?P<t>.+?)\s+"
               rf"(?:da\s+|de\s+)?{_ADD_VERB}"),
    re.compile(rf"^(?P<t>.+?)\s+(?:başlığını|maddesini|konusunu)\s+(?:da\s+|de\s+)?gündem(?:e|imize)\s+{_ADD_VERB}"),
]
# Başlığın sonundaki belirtme durumu eki ("gecikmelerini" → "gecikmeleri", "bütçeyi" → "bütçe")
_ACCUSATIVE = re.compile(r"(?<=[aeıioöuü])(?:n[ıiuü]|y[ıiuü])$")


def _low(text: str) -> str:
    return " ".join(tr_lower(text or "").replace("’", "'").split())


def request_clause(command: str) -> str | None:
    """Bağlam + istek içeren cümlede (ör. "X bitti, Y'ye geçelim") tek istek yan cümlesini döndürür.

    Birden fazla istek varsa veya istek yoksa None (çoklu işlem ön denetimde ayrıca ele alınır).
    """
    clauses = [c.strip(" .!?") for c in _CLAUSES.split(_low(command)) if c.strip(" .!?")]
    if len(clauses) < 2:
        return None
    requests = [c for c in clauses if _REQUEST_FORM.search(c)]
    if len(requests) != 1:
        return None
    # Bağlam yan cümlesi yüklemli bir cümle olmalı ("X'in konuşması bitti"). Virgülle ayrılmış ad listesi
    # ("Burcu, Kerem ve Emre'yi ekle") bağlam DEĞİLDİR; bölünürse adlar kaybolur (D14 gerçek bulgusu).
    if any(not _PREDICATE.search(c) for c in clauses if c is not requests[0]):
        return None
    return requests[0]


def has_write_request(command: str) -> bool:
    return bool(_WRITE_REQUEST.search(_low(command)))


# Katılım BİLDİRİMİ ("Kerem ve Nihat geldi") bir sorgu değil, yoklamaya kayıttır (D33 gerçek bulgusu: model
# ATTENDANCE seçti ve okuma çalıştı). Soru biçimi ("kimler geldi?") okumadır.
_ATTENDANCE_REPORT = re.compile(r"\b(?:geldi(?:ler)?|katıldı(?:lar)?|gelmiş(?:ler)?|katılmış(?:lar)?)\b")
_QUESTION = re.compile(r"\?|\bm[ıiuü](?:sın|sin|yız|yiz|dır|dir)?\b|\b(?:kim\w*|hangi\w*|kaç\w*|ne\w*)\b")


def is_attendance_report(command: str) -> bool:
    low = _low(command)
    return bool(_ATTENDANCE_REPORT.search(low)) and not _QUESTION.search(low)


# Geçmiş toplantı sorusu (HR19): bilgi veritabanındaki kayıttan okunur (SUMMARIZE_PREVIOUS), uydurulmaz
_PAST_MEETING = re.compile(r"\b(?:geçen|önceki|bir önceki|evvelki|son|dünkü)\s+(?:haftaki\s+|ayki\s+)?toplantı\w*")
_PAST_CONTENT = re.compile(r"\b(?:özet\w*|konuş\w*|görüş\w*|karar\w*|neler|ne oldu|hatırlat\w*|anlat\w*|"
                           r"ne(?:ler)? (?:yaptık|çıktı))")
_CURRENT_MEETING = re.compile(r"\b(?:bu|şimdiki|bugünkü|şu anki)\s+toplantı\w*|\bbugün\b")


def refers_to_past_meeting(command: str) -> bool:
    return bool(_PAST_MEETING.search(_low(command)))


def lexical_intent(command: str) -> IntentType | None:
    """Sözcük ipucundan tek ve açık bir niyet çıkarılabiliyorsa onu döndürür; değilse None."""
    low = _low(command)
    found = []
    past = bool(_PAST_MEETING.search(low))
    if past and _PAST_CONTENT.search(low) and not _WRITE_REQUEST.search(low):
        found.append(IntentType.SUMMARIZE_PREVIOUS)
    if _NEXT_ITEM.search(low):
        found.append(IntentType.NEXT_ITEM)
    if _RELEASE.search(low):
        found.append(IntentType.RELEASE_SPEAKER)
    if _ADD_AGENDA.search(low) and agenda_title(command):
        found.append(IntentType.ADD_AGENDA_ITEM)
    elif (_READ_AGENDA.search(low) and not _WRITE_REQUEST.search(low) and not _NEXT_ITEM.search(low)
          and not past):  # geçmiş toplantının konuları bugünkü gündem değildir
        found.append(IntentType.READ_AGENDA)
    if _ATTENDANCE_NOUN.search(low) and _WRITE_REQUEST.search(low):
        found.append(IntentType.MARK_PRESENT_MANY)
    return found[0] if len(found) == 1 else None


def agenda_title(command: str) -> str | None:
    """Gündeme eklenecek başlığı kullanıcının sözcükleriyle çıkarır (uydurma yok; bulunamazsa None)."""
    original = " ".join((command or "").replace("’", "'").split())
    low = tr_lower(original)
    if len(low) != len(original):
        original = low
    for pattern in _TITLE_PATTERNS:
        match = pattern.search(low)
        if match:
            start, end = match.span("t")
            title = original[start:end].strip(" ,.:;'\"")
            words = title.split()
            # Ek yalnızca başlık doğrudan fiilin nesnesiyse atılır ("gecikmelerini ekle"); "düzeni maddesini" gibi
            # tamlamalarda başlık yalın hâldedir ve değiştirilmez.
            noun_follows = re.match(r"\s+(?:başlı|madde|konu)", low[end:])
            if words and not noun_follows and len(words[-1]) > 4:
                words[-1] = _ACCUSATIVE.sub("", words[-1])
            title = " ".join(words)
            if title and not re.fullmatch(r"(?:bunu|şunu|onu|bir şey|bir madde|madde|başlık|konu)", tr_lower(title)):
                return title
    return None


WRITE_COUNTERPART = {IntentType.ATTENDANCE: IntentType.MARK_PRESENT_MANY,
                     IntentType.READ_AGENDA: IntentType.ADD_AGENDA_ITEM}


# Yoklama cümlesinde ad listesinin bittiği yer ("... yoklamaya ekle", "... de geldi")
_ATTENDANCE_ANCHOR = re.compile(
    r"\s+(?:(?:de|da)\s+)?(?:yoklamaya|katılımcılara|katılımcı olarak|listeye|aramıza|aramızda|toplantıya)\b"
    r"|\s+(?:(?:de|da)\s+)?(?:geldi|katıldı|gelmiş|katılmış|burada)\w*"
    r"|\s+(?:ekle|kaydet|işaretle)\w*(?:\s+m[ıiuü]s[ıiuü]n\w*)?$")
_ATTENDANCE_WORD = re.compile(r"\b(?:yoklama\w*|katılımcı\w*|liste\w*|toplantı\w*)\b")
_NAME_FILLERS = re.compile(r"^(?:misafirimiz|misafir|konuğumuz|konuk|arkadaşımız|sayın|bir de|ayrıca)\s+",
                           re.IGNORECASE)


def heard_attendance_names(command: str) -> list[str] | None:
    """Yoklama cümlesindeki ad listesini kullanıcının sözcükleriyle çıkarır (modelden bağımsız denetim için).

    Liste sınırı bulunamazsa None. Adlar ekinden ayrılır ("Emre'yi" → "Emre"); çözümleme yapılmaz.
    """
    from ..utils.turkish_text import split_names, strip_honorifics, strip_name_suffix  # noqa: PLC0415

    text = " ".join((command or "").replace("’", "'").split())
    low = tr_lower(text)
    match = _ATTENDANCE_ANCHOR.search(low)
    if not match or len(low) != len(text):
        return None
    names = []
    for chunk in split_names(text[:match.start()]):
        chunk = strip_honorifics(_NAME_FILLERS.sub("", chunk.strip()))
        words = chunk.split()
        if not words or len(words) > 3 or _ATTENDANCE_WORD.search(tr_lower(chunk)):
            return None  # ad listesi değil (ör. tam bir cümle veya "yoklamaya"): denetim yapılmaz
        if any(w[:1].islower() for w in words[1:]):
            return None  # "Mehmet az önce": ad dışında sözcük var; güvenilir liste çıkarılamaz
        words[-1] = strip_name_suffix(words[-1])
        names.append(" ".join(words))
    return names or None


# Katılmayanlar (yoklama) ile söz almayanlar (konuşma) farklı bilgilerdir (HR17 gerçek bulgusu: "kimler henüz
# gelmedi" → SILENT_PARTICIPANTS). Yanlış okuma niyeti yanlış bilgi verir; sözcüklerle tutarlılık denetlenir.
_ATTEND_WORDS = re.compile(r"\b(?:gel\w*|katıl\w*|eksik\w*|devamsız\w*|burada\w*|yok(?:lar)?|toplantıda\w*)\b")
_SPEAK_WORDS = re.compile(r"\b(?:konuş\w*|söz\w*|fikr\w*|sesini|görüş bildir\w*)\b")


def consistent_read_intent(intent_type: IntentType, command: str) -> IntentType:
    low = _low(command)
    attend, speak = bool(_ATTEND_WORDS.search(low)), bool(_SPEAK_WORDS.search(low))
    if intent_type == IntentType.SILENT_PARTICIPANTS and attend and not speak:
        return IntentType.ATTENDANCE
    if intent_type == IntentType.ATTENDANCE and speak and not attend and not _PAST_MEETING.search(low):
        return IntentType.SILENT_PARTICIPANTS
    # Geçmiş toplantının konuları/kararları sorulurken bugünkü gündem okunmaz (HR19)
    if intent_type == IntentType.READ_AGENDA and _PAST_MEETING.search(low) and not _CURRENT_MEETING.search(low):
        return IntentType.SUMMARIZE_PREVIOUS
    return intent_type


def past_present_conflict(intent_type: IntentType, command: str) -> bool:
    """Model önceki toplantı özetini seçti ama cümle bu toplantıyı soruyor: yanlış bilgi verilmez, sorulur."""
    low = _low(command)
    return (intent_type == IntentType.SUMMARIZE_PREVIOUS and bool(_CURRENT_MEETING.search(low))
            and not _PAST_MEETING.search(low))
