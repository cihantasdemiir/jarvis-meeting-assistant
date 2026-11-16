"""Yerel LLM bileşenlerinin ayarlardan kurulması.

LLM_ENABLED=false (varsayılan) iken hiçbir ağ isteği yapılmaz: komut yorumlama kapalıdır,
toplantı analizi kural tabanlı çalışır. LLM açıkken sunucu çalışmıyorsa veya model yüklü değilse
uygulama yine açılır; her LLM çağrısı hata verdiğinde kurallı sisteme geri dönülür.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .analyzer import MeetingAnalyzer
from .client import LlmError, LlmPolicyError, OllamaClient, check_privacy
from .router import LlmCommandRouter

log = logging.getLogger(__name__)


@dataclass
class LlmServices:
    client: Any
    model: str
    router: LlmCommandRouter | None
    analyzer: MeetingAnalyzer
    qa_timeout_s: float = 45.0
    confirm_timeout_s: float = 60.0
    status: str = ""

    @property
    def enabled(self) -> bool:
        return self.client is not None


def disabled_services(status: str = "Yerel LLM kapalı (LLM_ENABLED=false); kural tabanlı çalışılıyor.",
                      chunk_chars: int = 6000) -> LlmServices:
    return LlmServices(client=None, model="", router=None, analyzer=MeetingAnalyzer(chunk_chars=chunk_chars),
                       status=status)


def build_llm_services(settings, parser, client: Any = None, probe: bool = True) -> LlmServices:
    if not settings.llm_enabled:
        return disabled_services(chunk_chars=settings.llm_chunk_chars)
    try:
        check_privacy(settings.ollama_host, settings.llm_model, settings.llm_allow_remote)
        client = client or OllamaClient.from_settings(settings)
    except LlmPolicyError as exc:
        log.warning("LLM devre dışı: %s", exc)
        return disabled_services(f"Yerel LLM devre dışı: {exc}", settings.llm_chunk_chars)
    status = f"Yerel LLM: {settings.llm_model} ({settings.ollama_host})."
    if probe:
        try:
            if not client.has_model(settings.llm_model, timeout=3.0):
                status += (f" UYARI: model yüklü değil; 'ollama pull {settings.llm_model}' çalıştırın. "
                           "Bu arada kural tabanlı çalışılıyor.")
        except LlmError as exc:
            status += f" UYARI: sunucuya ulaşılamadı ({exc}). LLM çağrıları kurallı sisteme geri düşecek."
    router = (LlmCommandRouter(client, settings.llm_model, parser, timeout_s=settings.llm_router_timeout_s,
                               min_confidence=settings.llm_min_confidence)
              if settings.llm_router_enabled else None)
    analyzer = (MeetingAnalyzer(client, settings.llm_model, timeout_s=settings.llm_timeout_s,
                                chunk_chars=settings.llm_chunk_chars)
                if settings.llm_analysis_enabled else MeetingAnalyzer(chunk_chars=settings.llm_chunk_chars))
    log.info(status)
    return LlmServices(client=client, model=settings.llm_model, router=router, analyzer=analyzer,
                       qa_timeout_s=min(120.0, settings.llm_timeout_s),
                       confirm_timeout_s=float(settings.llm_confirm_timeout_s), status=status)


def config_report(settings) -> list[str]:
    """Canlı oturumun LLM yapılandırmasını benchmark ile karşılaştırır (gizli değer içermez).

    Canlı testte "Yerel LLM kapalı" görülürken benchmark'ın çalışmasının nedeni: `--llm-benchmark`,
    LLM_ENABLED'a bakmadan LLM_COMPARE_MODELS modellerini doğrudan çağırır; canlı oturum ise yalnızca
    LLM_ENABLED=true iken ve LLM_MODEL ile çalışır.
    """
    lines = [f".env: {settings.env_status or 'bilinmiyor'}"]
    compare = ", ".join(settings.llm_compare_models) or "-"
    if not settings.llm_enabled:
        lines.append("Yerel LLM: KAPALI (LLM_ENABLED=false). Kurallarla anlaşılamayan komutlar «anlayamadım» "
                     "yanıtı alır.")
        lines.append(f"  Not: --llm-benchmark bu ayara bakmaz ({compare} modellerini doğrudan dener). Canlı oturumda "
                     f"kullanmak için .env içinde LLM_ENABLED=true ve LLM_MODEL=<benchmark'taki model> yazın "
                     f"(şu an LLM_MODEL={settings.llm_model}).")
        return lines
    if not settings.llm_router_enabled:
        lines.append("Yerel LLM açık ama komut yorumlama kapalı (LLM_ROUTER_ENABLED=false).")
    if settings.llm_model not in settings.llm_compare_models:
        lines.append(f"UYARI: Canlı model LLM_MODEL={settings.llm_model}, benchmark modelleri ({compare}) "
                     "arasında değil; benchmark sonuçları bu modele ait değildir.")
    else:
        lines.append(f"Canlı model LLM_MODEL={settings.llm_model} (benchmark listesinde).")
    return lines
