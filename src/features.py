"""Calcolo feature tecniche da una serie di candele Capital.com.

Le candele Capital.com hanno questa forma:
{
  "snapshotTime": "2026/04/17 08:00:00",
  "openPrice":  {"bid": ..., "ask": ...},
  "closePrice": {"bid": ..., "ask": ...},
  "highPrice":  {"bid": ..., "ask": ...},
  "lowPrice":   {"bid": ..., "ask": ...},
  "lastTradedVolume": ...
}
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _mid(price_obj: dict[str, float]) -> float:
    return (price_obj["bid"] + price_obj["ask"]) / 2


def _to_arrays(candles: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    return {
        "open": np.array([_mid(c["openPrice"]) for c in candles]),
        "high": np.array([_mid(c["highPrice"]) for c in candles]),
        "low": np.array([_mid(c["lowPrice"]) for c in candles]),
        "close": np.array([_mid(c["closePrice"]) for c in candles]),
        "volume": np.array(
            [c.get("lastTradedVolume", 0) or 0 for c in candles], dtype=float
        ),
    }


def _rsi(close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return 50.0
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _atr(arr: dict[str, np.ndarray], period: int = 14) -> float:
    if len(arr["close"]) < period + 1:
        return 0.0
    high, low, close = arr["high"], arr["low"], arr["close"]
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )
    return float(tr[-period:].mean())


def _trend_strength(close: np.ndarray) -> float:
    """Pendenza normalizzata della regressione lineare sulle ultime 20 candele.
    Valori positivi = trend rialzista, negativi = ribassista."""
    if len(close) < 20:
        return 0.0
    window = close[-20:]
    x = np.arange(len(window))
    slope, _ = np.polyfit(x, window, 1)
    return float(slope / window.mean() * 100)  # in percentuale del prezzo medio


def _bollinger_width(close: np.ndarray, period: int = 20) -> float:
    if len(close) < period:
        return 0.0
    window = close[-period:]
    sd = window.std()
    mean = window.mean()
    if mean == 0:
        return 0.0
    return float((4 * sd) / mean * 100)  # ampiezza banda in % del prezzo


def compute_features(
    asset_name: str,
    candles_4h: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pacchettizza le feature da passare al LLM per scoring."""
    arr = _to_arrays(candles_4h)
    close = arr["close"]
    last = close[-1] if len(close) else None
    atr = _atr(arr)

    features: dict[str, Any] = {
        "last_price": round(float(last), 5) if last is not None else None,
        "rsi_14": round(_rsi(close), 1),
        "trend_slope_pct": round(_trend_strength(close), 3),
        "atr_4h": round(atr, 5),
        "atr_pct_of_price": round(atr / last * 100, 3) if last else None,
        "bb_width_pct": round(_bollinger_width(close), 3),
        "candles_used": len(close),
    }

    if last is not None and len(close) >= 20:
        features["high_20"] = round(float(close[-20:].max()), 5)
        features["low_20"] = round(float(close[-20:].min()), 5)
        features["pct_from_high_20"] = round(
            (last - features["high_20"]) / features["high_20"] * 100, 2
        )

    if snapshot:
        snap = snapshot.get("snapshot", snapshot)
        bid = snap.get("bid")
        ask = snap.get("offer") or snap.get("ask")
        if bid and ask:
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
            features["spread_pct"] = round(spread_pct, 4)

        features["market_status"] = snap.get("marketStatus", "UNKNOWN")

        # Momentum del giorno: Capital restituisce percentageChange gia'
        # calcolato sulla sessione corrente. Senza questo il LLM non ha
        # visibilita' sui mover intraday (es. alt-coin a +25% oggi).
        pct_change = snap.get("percentageChange")
        if pct_change is not None:
            try:
                features["daily_pct_change"] = round(float(pct_change), 2)
            except (TypeError, ValueError):
                pass
        daily_high = snap.get("high")
        daily_low = snap.get("low")
        if daily_high is not None and daily_low is not None and last:
            try:
                dh = float(daily_high)
                dl = float(daily_low)
                if dh > 0:
                    features["daily_range_pct"] = round((dh - dl) / dh * 100, 2)
                    features["pct_from_daily_high"] = round(
                        (last - dh) / dh * 100, 2
                    )
            except (TypeError, ValueError):
                pass

        # Dati per sizing/feasibility check
        rules = snapshot.get("dealingRules", {}) or {}
        min_size_field = rules.get("minDealSize", {}) or {}
        step_field = rules.get("minSizeIncrement") or {}
        instrument = snapshot.get("instrument", {}) or {}
        features["min_size"] = float(min_size_field.get("value", 0.01) or 0.01)
        features["size_step"] = float(
            step_field.get("value", features["min_size"]) or features["min_size"]
        )
        features["margin_factor"] = (
            float(instrument.get("marginFactor", 5) or 5) / 100.0
        )

    return features
