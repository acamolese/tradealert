"""Calcolo del sizing in base a un BUDGET DI MARGINE in EUR.

Semantica user-friendly: il budget per trade rappresenta quanti EUR
vengono bloccati dal ``disponibile per il trading`` di Capital per
quel trade. Per farsi un'idea:
- Indici/forex (margin_factor ~5%): budget 15 EUR -> leva 20x,
  esposizione ~300 EUR, rischio tipico con SL 1.5% ~ 4.5 EUR.
- Commodities (margin_factor ~10-20%): budget 15 EUR -> esposizione
  75-150 EUR.
- Crypto (margin_factor ~50-65%): budget 15 EUR -> esposizione ~23 EUR,
  leva minima ~1.5x.

Il rischio effettivo (perdita se scatta lo SL) dipende da size e
stop loss tecnico, NON dal budget margine.

API:
    SizingResult.size           -> size finale (None se rifiutata)
    SizingResult.notional       -> esposizione generata (size * prezzo)
    SizingResult.margin_estimate-> margine davvero bloccato (EUR) ~ budget
    SizingResult.risk_estimate  -> perdita se scatta lo SL (EUR)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SizingResult:
    size: float | None
    notional: float
    margin_estimate: float
    risk_estimate: float
    reason: str = ""


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def calculate_size(
    margin_budget: float,
    entry_price: float,
    margin_factor: float,
    min_size: float,
    size_step: float,
    stop_pct: float,
    available_margin: float | None = None,
    tolerance: float = 1.5,
) -> SizingResult:
    """``margin_budget``: EUR che vogliamo (al massimo) bloccare come
    margine su questo trade. La size viene dimensionata per impegnare
    ``~ margin_budget`` EUR di margine sul conto."""
    if entry_price <= 0 or margin_factor <= 0:
        return SizingResult(
            None, 0, 0, 0, reason="Prezzo o margin factor non validi"
        )

    step = size_step or min_size
    target_notional = margin_budget / margin_factor
    raw_size = target_notional / entry_price
    sized = _round_to_step(raw_size, step)
    if sized < min_size:
        sized = min_size

    notional = sized * entry_price
    margin_est = notional * margin_factor

    # Caso 1: la size minima del broker blocca piu' margine del budget scelto
    if margin_est > margin_budget * tolerance:
        return SizingResult(
            None,
            notional,
            margin_est,
            sized * entry_price * stop_pct / 100,
            reason=(
                f"Size minima {min_size} blocca margine {margin_est:.2f} EUR > "
                f"{tolerance}x budget {margin_budget:.2f} EUR. "
                f"Asset non accessibile a questo budget."
            ),
        )

    # Caso 2: margine richiesto maggiore del margine disponibile sul conto
    if available_margin is not None and margin_est > available_margin:
        return SizingResult(
            None,
            notional,
            margin_est,
            sized * entry_price * stop_pct / 100,
            reason=(
                f"Margine richiesto {margin_est:.2f} > disponibile "
                f"{available_margin:.2f} sul conto"
            ),
        )

    risk_est = sized * entry_price * stop_pct / 100
    return SizingResult(sized, notional, margin_est, risk_est)
