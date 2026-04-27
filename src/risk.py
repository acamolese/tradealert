"""Calcolo del sizing per trade su Capital.com con doppio vincolo:
budget di margine (EUR bloccati) + cap su perdita massima per trade.

Il margin factor effettivo dell'account NON e' ``instrument.marginFactor``
da ``/markets/{epic}`` (che resta a 100% statico per tutti gli strumenti):
deve essere derivato da ``/accounts/preferences.leverages`` come
``mf_eff = 1 / leverage[instrument_type]``. Vedi ``effective_margin_factor``.

Il sizing viene bloccato dal piu' restrittivo tra:
- size dal budget di margine: ``size_margin = (margin_budget / mf) / entry``
- size da max loss per trade: ``size_loss   = max_loss_eur / (entry * stop_pct/100)``

Se ``min_size`` del broker eccede entrambi i vincoli, il setup viene
scartato come ``non eseguibile entro risk cap``.

API:
    SizingResult.size           -> size finale (None se rifiutata)
    SizingResult.notional       -> esposizione generata (size * prezzo)
    SizingResult.margin_estimate-> margine davvero bloccato (EUR) ~ budget
    SizingResult.risk_estimate  -> perdita se scatta lo SL (EUR)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SizingResult:
    size: float | None
    notional: float
    margin_estimate: float
    risk_estimate: float
    reason: str = ""


def effective_margin_factor(
    market: dict[str, Any], leverages_map: dict[str, int] | None = None
) -> float:
    """Margin factor effettivo per questo strumento sull'account corrente.

    Prima fonte: leverage settata in ``/accounts/preferences`` per il tipo
    instrument (``COMMODITIES``, ``INDICES``, ``CURRENCIES``,
    ``CRYPTOCURRENCIES``, ``SHARES``, ``BONDS``, ``INTEREST_RATES``).
    Fallback: ``instrument.marginFactor`` da ``/markets/{epic}``, che su
    questo broker e' un valore statico di prodotto (100%) e va usato solo
    se le preferences non sono disponibili.
    """
    instrument = market.get("instrument", {}) or {}
    instr_type = instrument.get("type")
    if leverages_map and instr_type and leverages_map.get(instr_type):
        leverage = leverages_map[instr_type]
        if leverage > 0:
            return 1.0 / float(leverage)
    raw = instrument.get("marginFactor", 5) or 5
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return 0.05


def _floor_to_step(value: float, step: float) -> float:
    """Arrotonda verso il basso al multiplo di ``step`` piu' vicino.
    Floor (non round-half) garantisce che il sizing non oltrepassi mai
    il vincolo da cui e' stato derivato (max loss o margine budget)."""
    if step <= 0:
        return value
    import math

    return math.floor(value / step) * step


def calculate_size(
    margin_budget: float,
    entry_price: float,
    margin_factor: float,
    min_size: float,
    size_step: float,
    stop_pct: float,
    available_margin: float | None = None,
    tolerance: float = 1.5,
    max_loss_per_trade_eur: float | None = None,
) -> SizingResult:
    """``margin_budget``: EUR che vogliamo (al massimo) bloccare come
    margine su questo trade.

    ``max_loss_per_trade_eur``: se settato, la size finale viene cappata
    in modo che ``size * entry * stop_pct/100 <= max_loss``. Se anche
    ``min_size`` del broker comporta una perdita potenziale superiore al
    cap, il setup viene scartato.
    """
    if entry_price <= 0 or margin_factor <= 0:
        return SizingResult(
            None, 0, 0, 0, reason="Prezzo o margin factor non validi"
        )
    if stop_pct is None or stop_pct <= 0:
        return SizingResult(
            None, 0, 0, 0, reason="Stop loss % non valido"
        )

    step = size_step or min_size
    target_notional = margin_budget / margin_factor
    raw_size_budget = target_notional / entry_price

    risk_per_unit = entry_price * stop_pct / 100.0
    if max_loss_per_trade_eur is not None and risk_per_unit > 0:
        raw_size_loss = max_loss_per_trade_eur / risk_per_unit
        raw_size = min(raw_size_budget, raw_size_loss)
    else:
        raw_size = raw_size_budget

    sized = _floor_to_step(raw_size, step)
    if sized < min_size:
        sized = min_size

    notional = sized * entry_price
    margin_est = notional * margin_factor
    risk_est = sized * risk_per_unit

    # Caso 1: cap perdita massima per trade superato dalla size minima broker
    if max_loss_per_trade_eur is not None and risk_est > max_loss_per_trade_eur:
        return SizingResult(
            None,
            notional,
            margin_est,
            risk_est,
            reason=(
                f"Size minima {min_size} comporta perdita potenziale "
                f"{risk_est:.2f} EUR > cap {max_loss_per_trade_eur:.2f} EUR. "
                f"Setup non eseguibile entro risk cap."
            ),
        )

    # Caso 2: la size minima del broker blocca piu' margine del budget scelto
    if margin_est > margin_budget * tolerance:
        return SizingResult(
            None,
            notional,
            margin_est,
            risk_est,
            reason=(
                f"Size minima {min_size} blocca margine {margin_est:.2f} EUR > "
                f"{tolerance}x budget {margin_budget:.2f} EUR. "
                f"Asset non accessibile a questo budget."
            ),
        )

    # Caso 3: margine richiesto maggiore del margine disponibile sul conto
    if available_margin is not None and margin_est > available_margin:
        return SizingResult(
            None,
            notional,
            margin_est,
            risk_est,
            reason=(
                f"Margine richiesto {margin_est:.2f} > disponibile "
                f"{available_margin:.2f} sul conto"
            ),
        )

    return SizingResult(sized, notional, margin_est, risk_est)
