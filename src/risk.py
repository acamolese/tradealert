"""Calcolo del sizing in base a un BUDGET DI MARGINE PER TRADE in EUR.

Logica (adatta a micro-capitale):
- Decidi quanta cifra in EUR vuoi impegnare come margine per ogni trade
  (es. 10-20 EUR su un capitale di 60-200 EUR).
- Da li' calcoliamo la size massima ammessa data la margin factor del
  broker. Se la size minima del broker richiederebbe piu' del budget,
  tolleriamo fino a 1.5x (configurabile via tolerance).
- Lo stop loss tecnico determina poi il rischio in EUR del trade.
  Il rischio cresce o cala in base alla size, NON al budget margine.

API:
    SizingResult.size           -> size finale (None se rifiutata)
    SizingResult.notional       -> esposizione in valuta (size * prezzo)
    SizingResult.margin_estimate-> margine impegnato stimato (EUR)
    SizingResult.risk_estimate  -> perdita stimata se scatta lo stop (EUR)
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
    """margin_budget: EUR che vogliamo (al massimo) impegnare per questo trade."""
    if entry_price <= 0 or margin_factor <= 0:
        return SizingResult(None, 0, 0, 0, reason="Prezzo o margin factor non validi")

    step = size_step or min_size
    target_notional = margin_budget / margin_factor
    raw_size = target_notional / entry_price
    sized = _round_to_step(raw_size, step)
    if sized < min_size:
        sized = min_size

    notional = sized * entry_price
    margin_est = notional * margin_factor

    # Caso 1: la size minima del broker richiede piu' del budget oltre tolleranza
    if margin_est > margin_budget * tolerance:
        return SizingResult(
            None,
            notional,
            margin_est,
            sized * entry_price * stop_pct / 100,
            reason=(
                f"Min size {min_size} impegna {margin_est:.2f} EUR > "
                f"{tolerance}x budget {margin_budget:.2f} EUR. "
                f"Asset non economicamente accessibile per questo budget."
            ),
        )

    # Caso 2: il margine richiesto sfora il margine effettivamente disponibile
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
