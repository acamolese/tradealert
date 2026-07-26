"""Sprint 9 — Protezione del profitto sul controller v2 (pre-registrato in
docs/sprint9-profit-lock.md). Sola lettura.

Simula il controller v2 su US500 daily con e senza trailing di protezione, e
confronta rendimento/drawdown/Sharpe. Risponde: proteggere il guadagno tutela o
erode?

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/profit_lock_test.py
"""
from __future__ import annotations

import csv
import gzip
import math
import statistics
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "candles" / "US500_DAY.csv.gz"
LAMBDA = 0.94
SIGMA_TARGET = 0.15
MAX_SCALE = 2.0
WARMUP = 250
FIN_DAILY = 0.00025        # financing sull'esposizione
SPREAD = 0.00008          # ~0.8 bps sul turnover
TRAILS = [0.03, 0.05, 0.08]
ANN = math.sqrt(252)


def load_closes():
    closes = []
    with gzip.open(DATA, "rt") as f:
        for r in csv.DictReader(f):
            try:
                cb, ca = float(r["close_bid"]), float(r["close_ask"])
            except (ValueError, KeyError, TypeError):
                continue
            closes.append((cb + ca) / 2)
    return closes


def rolling_sigma(closes):
    """sigma EWMA annualizzata al giorno t, usando i rendimenti fino a t (incluso)."""
    sig = [None] * len(closes)
    var = None
    for i in range(1, len(closes)):
        r = math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0
        var = r * r if var is None else LAMBDA * var + (1 - LAMBDA) * r * r
        if i >= WARMUP and var > 0:
            sig[i] = math.sqrt(var * 252)
    return sig


def simulate(closes, sig, trail=None):
    """Ritorna (equity_curve, daily_returns, days_flat). Esposizione = volatility
    targeting; se trail e' impostato, va flat quando il prezzo ritraccia oltre trail
    dal picco dall'ultimo ingresso, e rientra su nuovo massimo."""
    eq = 1.0
    curve, rets = [1.0], []
    in_mkt = True
    peak = closes[WARMUP]
    exit_ref = None   # prezzo del picco da cui si e' usciti (rientro sopra questo)
    prev_w = 0.0
    days_flat = 0
    for i in range(WARMUP + 1, len(closes)):
        if sig[i - 1] is None:
            curve.append(eq); rets.append(0.0); continue
        ret = closes[i] / closes[i - 1] - 1.0
        w_base = min(max(SIGMA_TARGET / sig[i - 1], 0.0), MAX_SCALE)

        if trail is not None:
            if in_mkt:
                peak = max(peak, closes[i - 1])
                if closes[i - 1] < peak * (1 - trail):
                    in_mkt = False
                    exit_ref = peak
            else:
                # rientra quando il prezzo recupera un nuovo massimo sopra il picco d'uscita
                if exit_ref and closes[i - 1] >= exit_ref:
                    in_mkt = True
                    peak = closes[i - 1]
            w = w_base if in_mkt else 0.0
        else:
            w = w_base

        gross = w * ret
        cost = w * FIN_DAILY + abs(w - prev_w) * SPREAD
        r_net = gross - cost
        eq *= (1 + r_net)
        curve.append(eq); rets.append(r_net)
        if w == 0.0:
            days_flat += 1
        prev_w = w
    return curve, rets, days_flat


def stats(curve, rets):
    cum = curve[-1] - 1.0
    sharpe = (statistics.mean(rets) / statistics.pstdev(rets) * ANN) if statistics.pstdev(rets) > 0 else float("nan")
    peak = -1e9; mdd = 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return cum, sharpe, mdd


def main() -> int:
    closes = load_closes()
    sig = rolling_sigma(closes)
    print(f"US500 daily: {len(closes)} barre | test dal giorno {WARMUP}\n")
    print(f"{'variante':<12} {'rend.cum':>9} {'Sharpe':>7} {'maxDD':>8} {'gg flat':>8}")
    print("-" * 48)

    results = {}
    for name, trail in [("puro", None)] + [(f"trail-{int(t*100)}%", t) for t in TRAILS]:
        curve, rets, flat = simulate(closes, sig, trail)
        cum, sh, mdd = stats(curve, rets)
        results[name] = (cum, sh, mdd, flat)
        print(f"{name:<12} {cum:>+8.1%} {sh:>7.2f} {mdd:>8.1%} {flat:>8}")

    # always (w=1) come beta di riferimento
    eq = 1.0; curve = [1.0]; rets = []; prev = 0.0
    for i in range(WARMUP + 1, len(closes)):
        ret = closes[i] / closes[i - 1] - 1.0
        cost = 1.0 * FIN_DAILY + abs(1.0 - prev) * SPREAD
        eq *= (1 + ret - cost); curve.append(eq); rets.append(ret - cost); prev = 1.0
    cum, sh, mdd = stats(curve, rets)
    print(f"{'always(w=1)':<12} {cum:>+8.1%} {sh:>7.2f} {mdd:>8.1%} {0:>8}")

    # verdetto
    p_cum, p_sh, p_mdd, _ = results["puro"]
    useful = False
    for name in [f"trail-{int(t*100)}%" for t in TRAILS]:
        c, s, d, _ = results[name]
        if s >= p_sh and d > p_mdd and (d - p_mdd) / abs(p_mdd) >= 0.20:  # DD meno profondo di >=20%
            useful = True
    harmful = all(results[f"trail-{int(t*100)}%"][0] < p_cum and results[f"trail-{int(t*100)}%"][1] <= p_sh for t in TRAILS)
    print("\n=== VERDETTO (gate pre-registrato) ===")
    if useful:
        v = "PROTEZIONE UTILE (Sharpe>=puro e DD migliore >=20%) -> valutare deploy"
    elif harmful:
        v = "PROTEZIONE DANNOSA (erode rendimento, Sharpe<=puro) -> non mettere"
    else:
        v = "TRADE-OFF (riduce DD ma erode rendimento) -> scelta di tolleranza utente"
    print(f">>> {v} <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
