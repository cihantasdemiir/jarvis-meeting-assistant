"""Girdi zinciri tanılaması (Faz 5c.2 canlı düzeltme).

Her girdi için hangi aşamada sonuçlandığı kaydedilir:
    STT (ses/klavye) → hitap (Jarvis) → kurallı ayrıştırıcı → diyalog → yerel LLM yönlendirici → sonuç

Gizlilik: varsayılan olarak SÖYLENEN METİN YAZILMAZ. Yalnızca sözcük/karakter sayısı, oturuma özel tuzla
alınmış kısa bir özet (aynı oturumdaki tekrarları görmek için; başka oturumla eşleştirilemez), niyet adları ve
aşama sonuçları yazılır. PIPELINE_TRACE_TEXT=true ile metin de yazılır (yalnızca yerel hata ayıklama için).
Dosya: logs/pipeline_trace.jsonl. Özet: `python main.py --trace-report`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TRACE_FILE = "pipeline_trace.jsonl"
MAX_BYTES = 5 * 1024 * 1024
UNKNOWN_PREFIX = "Komutu anlayamadım"


class PipelineTrace:
    def __init__(self, path: Path | None, include_text: bool = False, llm_enabled: bool = False,
                 llm_status: str = ""):
        self.path = path
        self.include_text = include_text
        self.llm_enabled = llm_enabled
        self.llm_status = llm_status
        self._salt = os.urandom(8)
        self._lock = threading.Lock()
        self.current: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def begin(self, text: object) -> None:
        if not self.enabled:
            return
        raw = str(text or "").strip()
        record: dict[str, Any] = {
            "t": datetime.now().replace(microsecond=0).isoformat(),
            "kaynak": "ses" if getattr(text, "captured_at", None) is not None else "klavye",
            "sozcuk": len(raw.split()), "karakter": len(raw),
            "ozet": hashlib.sha256(self._salt + raw.lower().encode("utf-8")).hexdigest()[:10],
            "llm": "açık" if self.llm_enabled else "kapalı",
        }
        confidence = getattr(text, "confidence", None)
        if confidence is not None:
            record["stt_guven"] = round(float(confidence), 2)
        latency = getattr(text, "stt_latency", None)
        if latency is not None:
            record["stt_sn"] = round(float(latency), 2)
        if self.include_text:
            record["metin"] = raw
        self.current = record

    def note(self, **fields: Any) -> None:
        if self.current is not None:
            self.current.update({k: v for k, v in fields.items() if v not in (None, "")})

    def finish(self, response: Any) -> None:
        record, self.current = self.current, None
        if record is None or self.path is None:
            return
        message = getattr(response, "message", "") or ""
        intent = getattr(response, "intent", None)
        record.update({
            "sonuc_niyet": getattr(intent, "value", None) or "-",
            "basarili": bool(getattr(response, "ok", False)),
            "komut": bool(getattr(response, "is_command", False)),
            "sonuc": _outcome(record, response, message),
        })
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_suffix(".jsonl.1"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:  # tanılama hiçbir koşulda toplantıyı durdurmaz
            log.exception("Tanılama kaydı yazılamadı")


def _outcome(record: dict[str, Any], response: Any, message: str) -> str:
    if not getattr(response, "is_command", True):
        return "komut_benzeri_isaretlendi" if record.get("transkript") == "komut_benzeri" else "transkript"
    if record.get("asama") == "diyalog":
        return "diyalog"
    if message.startswith(UNKNOWN_PREFIX):
        return "anlasilamadi"
    if "onaylıyor musunuz" in message or "«evet»" in message or "«Jarvis, evet»" in message:
        return "onay_istendi"
    return "islem_yapildi" if getattr(response, "ok", False) else "aciklama_veya_ret"


# ====================================================================== rapor
_LABELS = {
    "transkript": "Toplantı konuşması (transkripte yazıldı)",
    "komut_benzeri_isaretlendi": "Hitapsız komut kalıbı (işaretlendi)",
    "diyalog": "Diyalog yanıtı / takip sorusu",
    "anlasilamadi": "«Komutu anlayamadım»",
    "onay_istendi": "Onay istendi",
    "islem_yapildi": "Komut işlendi",
    "aciklama_veya_ret": "Açıklama istendi / reddedildi",
}


def trace_report(path: Path) -> str:
    """Kayıtları aşama aşama özetler. Metin içermez (kayıtta olsa bile rapora yazılmaz)."""
    if not path.exists():
        return f"Tanılama kaydı yok: {path}. .env içinde PIPELINE_TRACE=true olmalı; bir oturum çalıştırın."
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not records:
        return "Tanılama kaydı boş."
    total = len(records)
    lines = [f"Girdi zinciri tanılaması — {total} girdi ({records[0]['t']} → {records[-1]['t']})", ""]

    def block(title: str, counter: Counter, labels: dict[str, str] | None = None) -> None:
        lines.append(title)
        for key, count in counter.most_common():
            name = (labels or {}).get(key, key)
            lines.append(f"  {name:<45} {count:>5}  (%{100 * count / total:.0f})")
        lines.append("")

    block("Kaynak:", Counter(r.get("kaynak", "?") for r in records))
    block("Hitap (Jarvis):", Counter(r.get("hitap", "?") for r in records))
    block("Sonuç:", Counter(r.get("sonuc", "?") for r in records), _LABELS)
    unknown = [r for r in records if r.get("sonuc") == "anlasilamadi"]
    if unknown:
        block("«Anlayamadım» yanıtlarında kurallı ayrıştırıcı sonucu ve LLM durumu:",
              Counter(f"ayrıştırıcı={r.get('ayristirici', '-')}, llm={r.get('yonlendirici', r.get('llm', '?'))}"
                      for r in unknown))
    llm_off = sum(1 for r in records if r.get("llm") == "kapalı" and r.get("hitap") == "hitaplı")
    if llm_off:
        lines.append(f"UYARI: {llm_off} hitaplı girdi yerel LLM KAPALIYKEN işlendi (LLM_ENABLED=false veya .env "
                     "okunmadı). Kurallarla anlaşılamayan cümleler bu yüzden «anlayamadım» yanıtı aldı.")
    confidences = [r["stt_guven"] for r in records if "stt_guven" in r]
    if confidences:
        low = sum(1 for c in confidences if c < 0.6)
        lines.append(f"STT güveni: ortalama {sum(confidences) / len(confidences):.2f}; 0,60 altı {low} girdi.")
    return "\n".join(lines).rstrip()
