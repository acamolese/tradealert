"""Calcolo del sizing in base a un BUDGET DI ESPOSIZIONE in EUR.

Logica:
- Decidi quanta esposizione in EUR (notional) vuoi tenere su ogni trade.
  Esempio: con exposure_budget=10 su BTC @ 76k USD, la size punta a
  ~0.00013 BTC. Con exposure_budget=10 su un indice, la size e' molto
  piu' alta perche' il valore unitario e' minore. L'esposizione in EUR
  resta confrontabile tra asset diversi.
- Il MARGINE richiesto per quella esposizione dipende dal margin_factor
  del broker (es. 5% indici, 50-65% crypto) e viene solo riportato nella
  preview; non limita il sizing a meno che il margine disponibile sul
  conto non basti (caso 'margine insufficiente').
- Lo stop loss tecnico determina poi il rischio in EUR del trade,
  proporzionale a size * entry_price * stop_pct/100.

API:
    SizingResult.size           -> size finale (None se rifiutata)
    SizingResult.notional       -> esposizione effettiva in valuta
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
    exposure_budget: float,
    entry_price: float,
    margin_factor: float,
    min_size: float,
    size_step: float,
    stop_pct: float,
    available_margin: float | None = None,
    tolerance: float = 1.5,
) -> SizingResult:
    """``exposure_budget`` e' l'esposizione target in EUR (notional)."""
    if entry_price <= 0 or margin_factor <= 0:
        return SizingResult(
            None, 0, 0, 0, reason="Prezzo o margin factor non validi"
        )

    step = size_step or min_size
    raw_size = exposure_budget / entry_price
    sized = _round_to_step(raw_size, step)
    if sized < min_size:
        sized = min_size

    notional = sized * entry_price
    margin_est = notional * margin_factor

    # Caso 1: la size minima del broker esplode l'esposizione oltre tolleranza
    if notional > exposure_budget * tolerance:
        return SizingResult(
            None,
            notional,
            margin_est,
            sized * entry_price * stop_pct / 100,
            reason=(
                f"Min size {min_size} genera notional {notional:.2f} EUR > "
                f"{tolerance}x esposizione {exposure_budget:.2f} EUR. "
                f"Asset non accessibile a questa size."
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
