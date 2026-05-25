"""Tracciamento spesa Anthropic per ogni chiamata API.

Esposto come singola funzione ``log_usage(db, caller, response)``: viene
invocata dopo ogni ``client.messages.create`` e scrive una riga in
``llm_usage`` con tokens, modello e costo USD calcolato lato app.

Best effort: se la scrittura su Supabase fallisce non solleva, logga
solo un warning. Il chiamante non deve mai vedere un errore di logging.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# Prezzi in USD per milione di token. Aggiornare manualmente quando
# Anthropic cambia listino o quando aggiungiamo nuovi modelli. La chiave
# e' il prefisso del model id: il lookup fa startswith() in ordine.
# Fonte: https://docs.claude.com/en/docs/about-claude/pricing
_PRICES_PER_M = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4": (1.00, 5.00),
    "claude-sonnet-4-7": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
}
_DEFAULT_PRICE = (3.00, 15.00)  # fallback Sonnet-like


def _price_for(model: str) -> tuple[float, float]:
    for prefix, price in _PRICES_PER_M.items():
        if model.startswith(prefix):
            return price
    log.warning("llm_usage: modello sconosciuto %r, uso prezzi fallback", model)
    return _DEFAULT_PRICE


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Costo stimato in USD per una singola chiamata.

    Cache pricing Anthropic: cache write 5m TTL = 1.25x input base, write
    1h TTL = 2.0x, read = 0.10x. L'API non riporta il TTL usato nella
    write, quindi assumiamo 2.0x perche' lo scanner (oggi unico caller con
    caching) usa il TTL 1h: gli altri caller per ora non cachano. I
    ``input_tokens`` riportati dall'API NON includono i token serviti dalla
    cache, quindi si sommano.
    """
    in_price, out_price = _price_for(model)
    cost = (
        input_tokens * in_price
        + cache_creation_input_tokens * in_price * 2.00
        + cache_read_input_tokens * in_price * 0.10
        + output_tokens * out_price
    ) / 1_000_000
    return round(cost, 6)


def log_usage(config: Any, caller: str, response: Any) -> None:
    """Logga su DB l'uso di una chiamata Anthropic.

    ``response`` e' l'oggetto restituito da ``client.messages.create``.
    Costruisce internamente un ``Database`` dal ``config`` per non
    obbligare i chiamanti a propagarne uno. Best effort: non rilancia.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        model = getattr(response, "model", "") or ""
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_creation = int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost_usd = estimate_cost_usd(
            model,
            input_tokens,
            output_tokens,
            cache_creation,
            cache_read,
        )
        from .db import Database  # import locale: evita ciclo a import time

        db = Database(config)
        db.insert_llm_usage(
            {
                "caller": caller,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "cost_usd": cost_usd,
            }
        )
    except Exception:
        log.warning("llm_usage: log fallito per caller=%s", caller, exc_info=True)
