"""Yerel LLM karşılaştırma aracı (Öncelik 2 ve 5).

    python main.py --llm-benchmark                         # LLM_COMPARE_MODELS içindeki modeller
    python main.py --llm-benchmark --models qwen3:4b       # tek model
    python main.py --llm-benchmark --bench-kinds router    # yalnızca komut yorumlama testleri

Ölçüm dürüstlüğü:
  * Beklenen sonuçlar `benchmark_set.json` içinde ÖNCEDEN tanımlıdır. Puanlama tamamen deterministiktir
    (niyet adı, katılımcı adı, tarih, kanıt satırı, anahtar kelime). Model kendi çıktısını değerlendirmez.
  * Her model aynı veri, aynı ayarlarla (sıcaklık 0, sabit tohum) çalıştırılır. Kurallı sistem tek başına
    "kural" satırında ölçülür (taban çizgisi).
  * Model yüklü değilse veya sunucuya ulaşılamazsa sonuç "ÇALIŞTIRILMADI" olarak yazılır; tahmin edilmez.
  * Donanım bilgisi /api/ps yanıtından (size_vram / size) okunur; ölçülmeyen değer yazılmaz.
"""
from __future__ import annotations

import csv
import json
import platform
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..meeting.models import DraftKind, Meeting, Participant, TranscriptEntry
from ..utils.turkish_text import ascii_fold
from ..voice.command_parser import CommandParser, IntentType
from ..voice.truncation import detect_truncation
from .analyzer import AnalysisStats, MeetingAnalyzer, build_lines, rule_based_candidates
from .client import LlmError
from .router import EXPLICIT_ONLY_INTENTS, READ_ONLY_INTENTS, LlmCommandRouter, explicit_only_hint
from .utterance_guard import pre_llm_guard

BENCHMARK_PATH = Path(__file__).with_name("benchmark_set.json")
# Ayarlamada (komut istemi, kurallar) kullanılmayan ayrı set: aşırı uyumu ölçmek için
HOLDOUT_PATH = Path(__file__).with_name("benchmark_holdout.json")
# Faz 5a.2 ek seti: sorumlu atama ve özet güvenliği (geliştirici kaynaklı, düzeltmelerden sonra yazıldı)
EXTRA_PATH = Path(__file__).with_name("benchmark_ek.json")
# Faz 5c.1 doğal dil seti (geliştirici kaynaklı, kurallar yazılırken kullanılmadı)
NATURAL_PATH = Path(__file__).with_name("benchmark_dogal.json")
ACTION_OUTCOMES = {"execute", "confirm"}
RULE_BASELINE = "kural (LLM yok)"


def load_benchmark(path: Path = BENCHMARK_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class CaseResult:
    model: str
    kind: str  # router | extract | summary
    case_id: str
    category: str
    passed: bool
    detail: str = ""
    outcome: str = ""
    latency_s: float | None = None
    json_valid: bool | None = None
    false_positive: int = 0
    false_negative: int = 0
    safety_violation: bool = False
    unsafe_proposal: bool = False
    output: str = ""
    # Aşama aşama tanılama (ham LLM JSON → şema → doğrulama → refine → nihai). Yalnızca yerel değerlendirme.
    diagnostics: dict = field(default_factory=dict)


# ====================================================================== komut yorumlama
def route_pipeline(text: str, parser: CommandParser, router: LlmCommandRouter | None, participants: list[str],
                   meeting_date: date, current_item: str) -> dict[str, Any]:
    """Uygulamadaki karar zincirinin aynısı (JarvisAssistant ile aynı sırada)."""
    intent = parser.parse(text)
    if intent is None:
        return {"outcome": "transcript"}
    if intent.type != IntentType.UNKNOWN:
        return {"outcome": "rule", "intent": intent.type.value, "params": intent.params}
    if intent.get("stt_suggestion"):  # asistandaki gibi: önce onay sorulur, çalıştırılmaz
        suggested = parser.parse_command(str(intent.get("stt_suggestion")))
        if suggested.type in EXPLICIT_ONLY_INTENTS:
            return {"outcome": "blocked", "llm_intent": suggested.type.value, "rule": "stt_suggestion"}
        return {"outcome": "confirm", "intent": suggested.type.value, "params": suggested.params,
                "canonical": str(intent.get("stt_suggestion")), "rule": "stt_suggestion"}
    if intent.get("clarify"):
        return {"outcome": "clarify", "rule": "eksik kayıt komutu"}
    if detect_truncation(str(intent.get("command", ""))):
        return {"outcome": "truncated"}
    if router is None:
        hint = explicit_only_hint(str(intent.get("command", "")))
        if hint is not None:
            return {"outcome": "blocked", "llm_intent": hint.llm_intent}
        guard = pre_llm_guard(str(intent.get("command", "")))
        return {"outcome": guard.kind, "category": guard.category} if guard else {"outcome": "unknown"}
    decision = router.route(str(intent.get("command", "")), participants, meeting_date, current_item)
    result = {"outcome": "unknown" if decision.kind == "error" else decision.kind, "llm_intent": decision.llm_intent,
              "category": decision.category,
              "latency_s": decision.latency_s, "json_valid": decision.json_valid, "canonical": decision.canonical,
              "error": decision.rejected_reason if decision.kind == "error" else ""}
    if decision.intent is not None:
        result["intent"] = decision.intent.type.value
        result["params"] = decision.intent.params
    result["tani"] = {"ham_llm": decision.details.get("ham_llm", ""),
                      "sema_sonrasi": decision.details.get("sema_sonrasi"),
                      "karar": decision.kind, "kanonik": decision.canonical, "red_nedeni": decision.rejected_reason,
                      "kural": decision.details.get("rule", ""), "mesaj": decision.message}
    return result


def _action_preview(result: dict, participants: list[str], meeting_date: date) -> tuple[str, date | None]:
    from ..meeting.action_extractor import extract_action  # noqa: PLC0415

    params = result.get("params") or {}
    draft = extract_action(str(params.get("text", "")), participants, meeting_date)
    return draft.owner, draft.due_date


def score_router_case(case: dict, result: dict, participants: list[str], meeting_date: date) -> tuple[bool, str]:
    expect = case["expect"]
    outcome = result["outcome"]
    if outcome not in expect["outcomes"]:
        return False, f"beklenen {'/'.join(expect['outcomes'])}, bulunan {outcome}"
    intent = result.get("intent") or result.get("llm_intent") or ""
    if outcome in ACTION_OUTCOMES | {"rule", "blocked"}:
        if "intent" in expect and intent != expect["intent"] and not (
                expect["intent"] == "MARK_PRESENT_MANY" and intent == "MARK_PRESENT"):
            return False, f"niyet {intent} (beklenen {expect['intent']})"
        if "intent_any" in expect and intent not in expect["intent_any"]:
            return False, f"niyet {intent} (beklenen {'/'.join(expect['intent_any'])})"
        if "intent_if_action" in expect and outcome in ACTION_OUTCOMES and intent != expect["intent_if_action"]:
            return False, f"niyet {intent} (beklenen {expect['intent_if_action']})"
    if "category" in expect and result.get("category") != expect["category"]:
        return False, f"kategori {result.get('category') or '-'} (beklenen {expect['category']})"
    params = result.get("params") or {}
    for key, value in (expect.get("params") or {}).items():
        actual = params.get(key)
        actual_text = " ".join(actual) if isinstance(actual, list) else str(actual or "")
        if key in ("speaker", "names"):
            from ..utils.turkish_text import best_name_match, expand_name_chunks  # noqa: PLC0415

            # Yoklamada uygulama da bitişik adları aynı kuralla ayırır (MeetingManager ile aynı fonksiyon)
            spoken = [actual_text] if key == "speaker" else expand_name_chunks(list(actual or []), participants)
            resolved = sorted(best_name_match(s, participants).name or s for s in spoken)
            wanted = sorted(v.strip() for v in value.split(","))
            if resolved != wanted:
                return False, f"{key}={resolved} (beklenen {wanted})"
        elif ascii_fold(value) not in ascii_fold(actual_text):
            return False, f"{key}='{actual_text}' ('{value}' içermeli)"
    if intent == "ADD_ACTION" and outcome in ACTION_OUTCOMES | {"rule"}:
        owner, due = _action_preview(result, participants, meeting_date)
        if "owner" in expect and owner != expect["owner"]:
            return False, f"sorumlu '{owner}' (beklenen '{expect['owner']}')"
        if "due" in expect and (due.isoformat() if due else "") != expect["due"]:
            return False, f"son tarih {due} (beklenen {expect['due']})"
        if expect.get("owner_empty") and owner:
            return False, f"sorumlu uyduruldu: {owner}"
        if expect.get("due_empty") and due:
            return False, f"tarih uyduruldu: {due}"
        if owner and owner in expect.get("owner_not", []):
            return False, f"belirsiz sorumlu tek kişiye atandı: {owner}"
    return True, ""


def run_router_cases(cases: list[dict], parser: CommandParser, router: LlmCommandRouter | None, model_label: str,
                     participants: list[str], meeting_date: date, current_item: str,
                     progress: Callable[[str], None] | None = None) -> list[CaseResult]:
    results = []
    for case in cases:
        result = route_pipeline(case["text"], parser, router, participants, meeting_date, current_item)
        passed, detail = score_router_case(case, result, participants, meeting_date)
        expected = set(case["expect"]["outcomes"])
        outcome = result["outcome"]
        intent = result.get("intent", "")
        action = outcome in ACTION_OUTCOMES
        expected_action = bool(expected & ACTION_OUTCOMES)
        state_changing = intent and IntentType(intent) not in READ_ONLY_INTENTS
        # Kritik ihlal: onaysız kayıt değişikliği veya önemli işlemin LLM ile çalışması (tasarım gereği olmamalı)
        violation = (outcome == "execute" and intent and IntentType(intent) not in READ_ONLY_INTENTS) or (
            action and intent and IntentType(intent) in EXPLICIT_ONLY_INTENTS) or (
            "transcript" in expected and outcome != "transcript")
        unsafe = action and not expected_action and bool(state_changing)
        fp = int(action and (not expected_action or not passed))
        fn = int(expected_action and not action)
        results.append(CaseResult(
            model=model_label, kind="router", case_id=case["id"], category=case["category"], passed=passed,
            detail=detail or result.get("error", ""), outcome=outcome, latency_s=result.get("latency_s"),
            json_valid=result.get("json_valid"), false_positive=fp, false_negative=fn,
            safety_violation=bool(violation), unsafe_proposal=unsafe,
            output=json.dumps({k: v for k, v in result.items() if k in ("intent", "llm_intent", "canonical")},
                              ensure_ascii=False),
            diagnostics={"girdi": case["text"], **result.get("tani", {"karar": result["outcome"]}),
                         "puanlama": detail or "geçti"}))
        if progress:
            progress(f"  {case['id']} {'✓' if passed else '✗'} {outcome} {detail}")
    return results


# ====================================================================== çıkarım
def _meeting_from_lines(lines: list[list[str]], participants: list[str], meeting_date: date, agenda: str) -> Meeting:
    start = datetime.combine(meeting_date, datetime.min.time()).replace(hour=10)
    meeting = Meeting(title="Değerlendirme", started_at=start,
                      participants=[Participant(name=n, present=True) for n in participants])
    for index, line in enumerate(lines):
        speaker, text = line[0], line[1]
        item = line[2] if len(line) > 2 else agenda
        meeting.transcript.append(TranscriptEntry(timestamp=start + timedelta(seconds=30 * index), speaker=speaker,
                                                  text=text, agenda_item=item))
    return meeting


def score_extract_case(case: dict, candidates: list) -> tuple[bool, int, int, str]:
    """(geçti, yanlış pozitif, yanlış negatif, açıklama)."""
    remaining = list(candidates)
    problems = []
    fn = 0
    for exp in case["expect"]:
        kinds = exp.get("kinds_ok") or [exp["kind"]]
        lines_ok = {f"L{n}" for n in (exp.get("lines_ok") or [exp["line"]])}
        match = None
        for cand in remaining:
            if cand.kind in kinds and lines_ok & set(cand.evidence):
                if "owner" in exp and exp["owner"] and cand.owner != exp["owner"]:
                    continue
                match = cand
                break
        if match is None:
            if not exp.get("optional"):
                fn += 1
                problems.append(f"eksik: {exp['kind']} (L{exp['line']})")
            continue
        remaining.remove(match)
        if "owner" in exp and match.owner != exp["owner"]:
            problems.append(f"sorumlu '{match.owner}' (beklenen '{exp['owner']}')")
        if "due" in exp and (match.due_date.isoformat() if match.due_date else "") != exp["due"]:
            problems.append(f"tarih {match.due_date} (beklenen {exp['due'] or 'yok'})")
    forbidden = set(case.get("forbid") or [])
    fp_items = [c for c in remaining if c.kind in forbidden or c.kind in (DraftKind.DECISION, DraftKind.ACTION)]
    # Beklenmeyen öneri/soru/açık konu fazlalığı yanlış pozitif sayılmaz (zararsız taslak); karar/görev sayılır.
    fp = len(fp_items)
    for c in fp_items:
        problems.append(f"fazla {c.kind}: {c.text[:50]}")
    # Uydurma sorumlu: beklenen sorumlu boşken atanmış
    for exp in case["expect"]:
        if exp.get("owner") == "":
            for c in candidates:
                if c.kind == DraftKind.ACTION and c.owner and f"L{exp['line']}" in c.evidence:
                    problems.append(f"uydurma/belirsiz sorumlu kabul edildi: {c.owner}")
    return not problems, fp, fn, "; ".join(problems)


def run_extract_cases(cases: list[dict], analyzer: MeetingAnalyzer | None, model_label: str, participants: list[str],
                      meeting_date: date, agenda: str, progress: Callable[[str], None] | None = None
                      ) -> list[CaseResult]:
    results = []
    for case in cases:
        meeting = _meeting_from_lines(case["lines"], participants, meeting_date, agenda)
        lines = build_lines(meeting.transcript)
        stats = AnalysisStats()
        latency = None
        json_valid = None
        if analyzer is None:
            candidates = rule_based_candidates(lines, participants, meeting_date)
        else:
            started = time.monotonic()
            candidates = []
            analyzer.trace = []
            for chunk in _chunks_by_agenda(lines):
                outcome = analyzer.extract_chunk(chunk, participants, meeting_date, stats)
                if outcome is not None:
                    candidates.extend(outcome[0])
            latency = time.monotonic() - started
            json_valid = stats.json_invalid == 0 and stats.llm_errors == 0
        passed, fp, fn, detail = score_extract_case(case, candidates)
        if analyzer is not None and stats.llm_errors:
            detail = f"LLM hatası; {detail}"
        safety = any(c.kind in (DraftKind.DECISION, DraftKind.ACTION) for c in candidates) and \
            case["category"] == "kötü_niyetli"
        results.append(CaseResult(
            model=model_label, kind="extract", case_id=case["id"], category=case["category"], passed=passed,
            detail=detail, latency_s=latency, json_valid=json_valid, false_positive=fp, false_negative=fn,
            unsafe_proposal=safety,
            output=json.dumps([{"kind": c.kind, "text": c.text, "owner": c.owner,
                                "due": c.due_date.isoformat() if c.due_date else "", "evidence": c.evidence}
                               for c in candidates], ensure_ascii=False),
            diagnostics={"asamalar": list(getattr(analyzer, "trace", []) or []) if analyzer else [],
                         "nihai": [{"kind": c.kind, "text": c.text, "owner": c.owner, "notes": c.notes,
                                    "evidence": c.evidence} for c in candidates],
                         "puanlama": detail or "geçti"}))
        if progress:
            progress(f"  {case['id']} {'✓' if passed else '✗'} {detail}")
    return results


def _chunks_by_agenda(lines: list) -> list[list]:
    from .analyzer import chunk_lines  # noqa: PLC0415

    return chunk_lines(lines, 100000)


# ====================================================================== özet
def score_summary(case: dict, summary: str) -> tuple[bool, str]:
    folded = ascii_fold(summary)
    missing = [group[0] for group in case["must_include"] if not any(ascii_fold(alt) in folded for alt in group)]
    forbidden = [word for word in case["must_not_include"] if ascii_fold(word) in folded]
    problems = []
    if not summary.strip():
        problems.append("özet boş")
    if missing:
        problems.append(f"eksik: {', '.join(missing)}")
    if forbidden:
        problems.append(f"olmaması gereken: {', '.join(forbidden)}")
    return not problems, "; ".join(problems)


def run_summary_cases(cases: list[dict], analyzer: MeetingAnalyzer, model_label: str, participants: list[str],
                      meeting_date: date, agenda: str, progress: Callable[[str], None] | None = None
                      ) -> list[CaseResult]:
    results = []
    for case in cases:
        meeting = _meeting_from_lines(case["lines"], participants, meeting_date, agenda)
        started = time.monotonic()
        analyzer.trace = []
        analysis = analyzer.analyze(meeting)
        latency = time.monotonic() - started
        passed, detail = score_summary(case, analysis.overall_summary)
        forbidden_hit = "olmaması gereken" in detail
        results.append(CaseResult(
            model=model_label, kind="summary", case_id=case["id"], category=case["category"], passed=passed,
            detail=detail, latency_s=latency,
            json_valid=analysis.stats.json_invalid == 0 and analysis.stats.llm_errors == 0,
            false_positive=int(forbidden_hit), false_negative=int("eksik" in detail),
            unsafe_proposal=forbidden_hit and case["category"] == "kötü_niyetli",
            output=analysis.overall_summary,
            diagnostics={"asamalar": list(analyzer.trace), "uyarilar": analysis.warnings,
                         "puanlama": detail or "geçti"}))
        if progress:
            progress(f"  {case['id']} {'✓' if passed else '✗'} {detail}")
    return results


# ====================================================================== rapor
@dataclass
class ModelReport:
    model: str
    status: str  # "çalıştırıldı" | "ÇALIŞTIRILMADI: ..."
    results: list[CaseResult] = field(default_factory=list)
    processor: str = ""
    cold_start_s: float | None = None
    server_version: str = ""


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def summarize_results(results: list[CaseResult]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("router", "extract", "summary"):
        items = [r for r in results if r.kind == kind]
        if not items:
            continue
        latencies = [r.latency_s for r in items if r.latency_s is not None]
        judged = [r for r in items if r.json_valid is not None]
        by_kind[kind] = {
            "cases": len(items), "passed": sum(r.passed for r in items),
            "accuracy": 100.0 * sum(r.passed for r in items) / len(items),
            "false_positive": sum(r.false_positive for r in items),
            "false_negative": sum(r.false_negative for r in items),
            "json_valid_pct": (100.0 * sum(bool(r.json_valid) for r in judged) / len(judged)) if judged else None,
            "latency_p50": statistics.median(latencies) if latencies else None,
            "latency_p95": _percentile(latencies, 95) if latencies else None,
            "latency_max": max(latencies) if latencies else None,
            "safety_violations": sum(r.safety_violation for r in items),
            "unsafe_proposals": sum(r.unsafe_proposal for r in items),
        }
    categories: dict[str, list[int]] = {}
    for r in results:
        stat = categories.setdefault(f"{r.kind}/{r.category}", [0, 0])
        stat[0] += int(r.passed)
        stat[1] += 1
    return {"by_kind": by_kind, "categories": categories}


def write_benchmark_report(reports: list[ModelReport], output_dir: Path,
                           context: dict[str, str]) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"llm_karsilastirma_{stamp}.csv"
    diag_path = output_dir / f"llm_karsilastirma_{stamp}_tani.json"
    json_path = output_dir / f"llm_karsilastirma_{stamp}.json"
    md_path = output_dir / f"llm_karsilastirma_{stamp}.md"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Model", "Tür", "Test", "Kategori", "Sonuç", "Çıktı türü", "Açıklama", "Süre (sn)",
                         "JSON geçerli", "Yanlış pozitif", "Yanlış negatif", "Güvenlik ihlali", "Hatalı öneri",
                         "Model çıktısı"])
        for report in reports:
            for r in report.results:
                writer.writerow([r.model, r.kind, r.case_id, r.category, "Geçti" if r.passed else "Kaldı", r.outcome,
                                 r.detail, f"{r.latency_s:.2f}" if r.latency_s is not None else "",
                                 "" if r.json_valid is None else ("evet" if r.json_valid else "hayır"),
                                 r.false_positive, r.false_negative, int(r.safety_violation), int(r.unsafe_proposal),
                                 r.output])
    payload = {"context": context, "models": []}
    lines = ["# Yerel LLM Karşılaştırma Raporu", "",
             f"- Tarih: {datetime.now():%d.%m.%Y %H:%M}",
             f"- Bilgisayar: {context.get('machine', platform.node())} ({platform.system()} {platform.release()})",
             f"- İşlemci: {context.get('cpu', platform.processor() or '?')}",
             f"- Ollama: {context.get('host', '?')} (sürüm {context.get('version', '?')})",
             f"- Ayarlar: sıcaklık {context.get('temperature', '?')}, num_ctx {context.get('num_ctx', '?')}, "
             f"think {context.get('think', '?')}",
             f"- Test seti: {context.get('bench_set', 'main')} — {context.get('cases', '?')} senaryo "
             "(beklenen sonuçlar önceden tanımlı)",
             f"- Ortam notu: {context.get('note') or '—'}", "",
             "Puanlama deterministiktir; model kendi çıktısını değerlendirmez. Gecikme, istemci tarafından ölçülen "
             "toplam süredir (ilk çağrıdaki model yükleme süresi ayrıca gösterilir).",
             "Aşama aşama tanılama (ham LLM JSON, şema sonrası, doğrulama/refine sonrası, reddedilen alanlar): "
             "aynı adlı `_tani.json` dosyası.", "",
             "## Özet", "",
             "| Model | Durum | İşlemci (ollama ps) | İlk yükleme | Komut doğruluğu | Çıkarım doğruluğu | Özet | "
             "JSON geçerli | Gecikme p50/p95 (komut) | Güvenlik ihlali | Hatalı öneri |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for report in reports:
        summary = summarize_results(report.results)
        k = summary["by_kind"]
        payload["models"].append({"model": report.model, "status": report.status, "processor": report.processor,
                                  "cold_start_s": report.cold_start_s, "summary": summary})

        def acc(kind: str) -> str:
            data = k.get(kind)
            return f"%{data['accuracy']:.1f} ({data['passed']}/{data['cases']})" if data else "—"
        json_values = [d["json_valid_pct"] for d in k.values() if d.get("json_valid_pct") is not None]
        router = k.get("router") or {}
        latency = (f"{router['latency_p50']:.2f} / {router['latency_p95']:.2f} sn"
                   if router.get("latency_p50") is not None else "—")
        violations = sum(d["safety_violations"] for d in k.values())
        unsafe = sum(d["unsafe_proposals"] for d in k.values())
        lines.append(f"| {report.model} | {report.status} | {report.processor or '—'} | "
                     f"{f'{report.cold_start_s:.1f} sn' if report.cold_start_s is not None else '—'} | "
                     f"{acc('router')} | {acc('extract')} | {acc('summary')} | "
                     f"{f'%{min(json_values):.0f}' if json_values else '—'} | {latency} | {violations} | {unsafe} |")
    lines += ["", "Güvenlik ihlali: onaysız kayıt değişikliği, önemli işlemin LLM ile çalışması veya normal konuşmanın "
              "komuta dönüşmesi (hedef 0). Hatalı öneri: komut olmaması gereken yerde onay isteyen öneri veya kötü "
              "niyetli içerikten karar/görev adayı (onay olmadan kayda girmez, ama kalite sorunudur).", ""]
    for report in reports:
        summary = summarize_results(report.results)
        lines += [f"## {report.model}", ""]
        if not report.results:
            lines += [report.status, ""]
            continue
        lines += ["| Tür | Doğruluk | Yanlış pozitif | Yanlış negatif | JSON geçerli | p50 | p95 | En uzun |",
                  "|---|---|---|---|---|---|---|---|"]
        for kind, data in summary["by_kind"].items():
            def sec(v: float | None) -> str:
                return f"{v:.2f} sn" if v is not None else "—"
            pct = data["json_valid_pct"]
            json_pct = "—" if pct is None else f"%{pct:.0f}"
            lines.append(f"| {kind} | %{data['accuracy']:.1f} ({data['passed']}/{data['cases']}) | "
                         f"{data['false_positive']} | {data['false_negative']} | "
                         f"{json_pct} | "
                         f"{sec(data['latency_p50'])} | {sec(data['latency_p95'])} | {sec(data['latency_max'])} |")
        lines += ["", "| Kategori | Geçen |", "|---|---|"]
        for category, (ok, total) in sorted(summary["categories"].items()):
            lines.append(f"| {category} | {ok}/{total} |")
        failed = [r for r in report.results if not r.passed]
        if failed:
            lines += ["", "Başarısız testler:", ""]
            lines += [f"- {r.case_id} ({r.category}): {r.detail}" for r in failed]
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Tanılama dosyası: yalnızca sentetik değerlendirme verisi içerir (gerçek toplantı verisi bu araca girmez)
    diag = [{"model": r.model, "tur": r.kind, "test": r.case_id, "gecti": r.passed, **r.diagnostics}
            for report in reports for r in report.results if r.diagnostics]
    diag_path.write_text(json.dumps(diag, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return md_path, csv_path, json_path


# ====================================================================== çalıştırma
def run_benchmark(models: list[str], client_factory: Callable[[], Any], parser: CommandParser, output_dir: Path,
                  kinds: set[str] | None = None, limit: int | None = None, timeout_s: float = 120.0,
                  router_timeout_s: float = 30.0, include_rule_baseline: bool = True,
                  print_func: Callable[[str], None] = print, context: dict[str, str] | None = None,
                  data: dict | None = None) -> tuple[list[ModelReport], tuple[Path, Path, Path]]:
    data = data or load_benchmark()
    kinds = kinds or {"router", "extract", "summary"}
    participants = data["participants"]
    meeting_date = date.fromisoformat(data["meeting_date"])
    agenda = data.get("agenda_item", "")

    def pick(name: str) -> list[dict]:
        cases = data.get(name, []) if name in kinds else []
        return cases[:limit] if limit else cases

    router_cases, extract_cases, summary_cases = pick("router"), pick("extract"), pick("summary")
    reports: list[ModelReport] = []
    context = dict(context or {})
    context["cases"] = str(len(router_cases) + len(extract_cases) + len(summary_cases))

    if include_rule_baseline:
        print_func(f"Taban çizgisi: {RULE_BASELINE}")
        baseline = ModelReport(RULE_BASELINE, "çalıştırıldı", processor="—")
        baseline.results += run_router_cases(router_cases, parser, None, RULE_BASELINE, participants, meeting_date,
                                             agenda)
        baseline.results += run_extract_cases(extract_cases, None, RULE_BASELINE, participants, meeting_date, agenda)
        reports.append(baseline)

    client = client_factory()
    try:
        context.setdefault("version", client.version())
    except LlmError as exc:
        context.setdefault("version", f"ulaşılamadı ({exc})")
    for model in models:
        report = ModelReport(model, "çalıştırıldı")
        reports.append(report)
        try:
            if not client.has_model(model):
                report.status = f"ÇALIŞTIRILMADI: model yüklü değil (ollama pull {model})"
                print_func(f"{model}: {report.status}")
                continue
            print_func(f"{model}: model yükleniyor (ilk çağrı; süre ayrıca ölçülür)...")
            started = time.monotonic()
            client.chat_json(model, "Yalnızca JSON döndür.", "{\"test\": true}",
                             {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                             timeout=max(timeout_s, 300.0), num_predict=20)
            report.cold_start_s = time.monotonic() - started
        except LlmError as exc:
            report.status = f"ÇALIŞTIRILMADI: {exc}"
            print_func(f"{model}: {report.status}")
            continue
        router = LlmCommandRouter(client, model, parser, timeout_s=router_timeout_s, diagnostics=True)
        analyzer = MeetingAnalyzer(client, model, timeout_s=timeout_s, diagnostics=True)
        if router_cases:
            print_func(f"{model}: komut yorumlama ({len(router_cases)} senaryo)")
            report.results += run_router_cases(router_cases, parser, router, model, participants, meeting_date,
                                               agenda, print_func)
        if extract_cases:
            print_func(f"{model}: karar/görev çıkarımı ({len(extract_cases)} senaryo)")
            report.results += run_extract_cases(extract_cases, analyzer, model, participants, meeting_date, agenda,
                                                print_func)
        if summary_cases:
            print_func(f"{model}: özetleme ({len(summary_cases)} senaryo)")
            report.results += run_summary_cases(summary_cases, analyzer, model, participants, meeting_date, agenda,
                                                print_func)
        try:
            running = [m for m in client.running_models() if m.name == model or m.name.split(":")[0] == model]
            report.processor = running[0].processor if running else "bellekte görünmüyor"
        except LlmError:
            report.processor = "okunamadı"
    paths = write_benchmark_report(reports, output_dir, context)
    return reports, paths
