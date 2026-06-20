"""
Wrapper fino sobre a API da Claude (SDK anthropic), compartilhado pelos
serviços de IA do ZayaRun (insight, prescrição, triagem, copiloto).

Princípios:
- Nunca quebra o request: qualquer falha vira None e o chamador cai em regras.
- Honesto sem chave: is_enabled() permite o chamador decidir o fallback.
- Cache opcional por chave para não repetir chamadas caras.
"""
import logging
import os

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_TTL = 60 * 60  # 1h


def has_key():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def is_enabled():
    """A IA está ligada para este ambiente? (mesma regra do Insight do dia)."""
    provider = (getattr(settings, "INSIGHT_PROVIDER", "auto") or "auto").lower()
    if provider == "rules":
        return False
    return provider == "claude" or (provider == "auto" and has_key())


def complete(system, user_text, max_tokens=500, cache_key=None, temperature=None):
    """
    Faz uma chamada de texto à Claude e devolve a string (ou None em falha).
    `cache_key` (opcional) evita repetir a mesma geração.
    """
    if cache_key:
        cached = cache.get(cache_key)
        if cached:
            return cached
    if not has_key():
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("Pacote 'anthropic' não instalado; IA indisponível.")
        return None

    try:
        client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente
        kwargs = {
            "model": getattr(settings, "INSIGHT_MODEL", "claude-opus-4-8"),
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception:  # noqa: BLE001
        logger.exception("Falha na chamada à Claude.")
        return None

    if text and cache_key:
        cache.set(cache_key, text, DEFAULT_TTL)
    return text or None
