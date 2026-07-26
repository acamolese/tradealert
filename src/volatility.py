"""Modello di volatilita' v2 (spec §3): EWMA sui rendimenti log giornalieri.

Un solo modello in v1. lambda fisso (RiskMetrics), warm-up 250 barre. GARCH, lambda
variabile, regimi: fuori scope (§13), ognuno un esperimento a se'.
"""
from __future__ import annotations

import math

from src.exposure_config import EWMA_LAMBDA, WARMUP_BARS, TRADING_DAYS


def log_returns(closes: list[float]) -> list[float]:
    """Rendimenti logaritmici giornalieri da una serie di chiusure."""
    out: list[float] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev and prev > 0 and cur and cur > 0:
            out.append(math.log(cur / prev))
    return out


def ewma_sigma(closes: list[float]) -> float | None:
    """Volatilita' annualizzata stimata via EWMA. Ritorna None se non c'e' abbastanza
    storia per rispettare il warm-up (§3): sotto WARMUP_BARS barre il controller
    resta a 0 blocchi.

        r_t     = ln(close_t / close_{t-1})
        var_t   = lambda * var_{t-1} + (1-lambda) * r_t^2
        sigma_t = sqrt(var_t * TRADING_DAYS)
    """
    if len(closes) < WARMUP_BARS + 1:
        return None
    rets = log_returns(closes)
    if len(rets) < WARMUP_BARS:
        return None
    # seed con il primo r^2; con lambda 0.94 dopo 250 barre il seed pesa ~0
    var = rets[0] * rets[0]
    for r in rets[1:]:
        var = EWMA_LAMBDA * var + (1.0 - EWMA_LAMBDA) * r * r
    if var <= 0:
        return None
    return math.sqrt(var * TRADING_DAYS)
