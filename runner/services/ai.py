"""
Wrapper fino sobre a IA, compartilhado pelos serviços do ZayaRun (insight,
prescrição, triagem, copiloto). Suporta dois provedores:

- **Gemini** (Google AI Studio) — GRATUITO, ligado por `GEMINI_API_KEY`.
- **Claude** (SDK anthropic) — ligado por `ANTHROPIC_API_KEY`.

Princípios:
- Nunca quebra o request: qualquer falha vira None e o chamador cai em regras.
- Honesto sem chave: is_enabled() permite o chamador decidir o fallback.
- Cache opcional por chave para não repetir chamadas caras.
"""
import logging
import os

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_TTL = 60 * 60  # 1h
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def gemini_key():
    return os.environ.get("GEMINI_API_KEY")


def anthropic_key():
    return os.environ.get("ANTHROPIC_API_KEY")


def has_key():
    """Existe alguma chave de IA configurada?"""
    return bool(gemini_key() or anthropic_key())


def provider():
    """
    Resolve o provedor ativo a partir de AISettings + chaves disponíveis.
    "auto" prioriza a opção GRATUITA (Gemini) e cai para a Claude.
    Retorna "gemini", "claude" ou None (sem IA → fallback por regras).
    """
    pref = (getattr(settings, "AI_PROVIDER", "auto") or "auto").lower()
    # Em "auto", um INSIGHT_PROVIDER=rules explícito também desliga a IA (compat).
    if pref in ("", "auto") and (
        getattr(settings, "INSIGHT_PROVIDER", "auto") or "auto"
    ).lower() == "rules":
        return None
    if pref == "rules":
        return None
    if pref == "gemini":
        return "gemini" if gemini_key() else None
    if pref == "claude":
        return "claude" if anthropic_key() else None
    # auto: a gratuita primeiro
    if gemini_key():
        return "gemini"
    if anthropic_key():
        return "claude"
    return None


def is_enabled():
    """A IA está ligada para este ambiente?"""
    return provider() is not None


def gemini_request(system, user_text, max_tokens=500, temperature=None):
    """
    Chamada crua ao Gemini. Retorna (text, debug) — text=None em falha, com
    `debug` legível (status/motivo) para diagnóstico. NÃO levanta exceção.
    """
    key = gemini_key()
    if not key:
        return None, "sem GEMINI_API_KEY"
    model = getattr(settings, "GEMINI_MODEL", "gemini-2.0-flash")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if temperature is not None:
        body["generationConfig"]["temperature"] = temperature
    try:
        resp = requests.post(
            GEMINI_ENDPOINT.format(model=model),
            params={"key": key}, json=body, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"erro de rede: {exc!r}"
    if resp.status_code == 429:
        return None, f"HTTP 429 cota/limite (RESOURCE_EXHAUSTED): {resp.text[:400]}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:400]}"
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        fb = data.get("promptFeedback")
        return None, f"sem candidates (promptFeedback={fb})"
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        return None, f"resposta vazia (finishReason={cands[0].get('finishReason')})"
    return text, "ok"


def _complete_gemini(system, user_text, max_tokens, temperature):
    text, debug = gemini_request(system, user_text, max_tokens, temperature)
    if text is None:
        logger.error("Gemini falhou: %s", debug)
    return text


def _complete_claude(system, user_text, max_tokens, temperature):
    import anthropic

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
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def complete(system, user_text, max_tokens=500, cache_key=None, temperature=None):
    """
    Faz uma chamada de texto à IA ativa e devolve a string (ou None em falha).
    `cache_key` (opcional) evita repetir a mesma geração.
    """
    if cache_key:
        cached = cache.get(cache_key)
        if cached:
            return cached

    prov = provider()
    if prov is None:
        return None

    try:
        if prov == "gemini":
            text = _complete_gemini(system, user_text, max_tokens, temperature)
        else:
            text = _complete_claude(system, user_text, max_tokens, temperature)
    except ImportError:
        logger.warning("Pacote 'anthropic' não instalado; IA indisponível.")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Falha na chamada à IA (%s).", prov)
        return None

    if text and cache_key:
        cache.set(cache_key, text, DEFAULT_TTL)
    return text or None
