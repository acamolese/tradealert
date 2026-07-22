"""Sprint 8 — Effetto della frequenza del trailing sulle uscite.
Pre-registrato in docs/sprint8-exit-frequency.md. Sola lettura (candle su disco).

Stessi ENTRY del v1-momentum (ritardo D=1, dedup congelato su K=1) per tutte le
condizioni: isola SOLO l'effetto uscita. Variabile K = ogni quante barre orarie il
trailing AGGIORNA lo stop (l'HIT e' controllato a ogni barra, lo stop e' congelato
tra un aggiornamento e l'altro). K=1 = ogni ora (fine), K=4 = ogni 4h (grosso).

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/exit_frequency_backtest.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import timedelta

from jobs.backtest_run import load, OFFSET_FN, OVERNIGHT_PCT, OVERNIGHT_DEFAULT
from jobs.scan_frequency_backtest import (
    EPICS, _prev_day_close, MAX_SPREAD_PCT, MIN_ABS_DAILY_PCT,
    STOP_ATR_MULT, MIN_STOP_PCT, TARGET_RR, COOLDOWN_H, MAX_HOLD_DAYS,
)

KS = [1, 2, 4]
GATE = {"edge": 0.10, "flat": 0.05, "robust": 0.05}


def simulate_exit_k(d, epic, j, direction, r_dist, rr, K):
    """Uscita col motore live, ma il trailing AGGIORNA l'offset solo ogni K barre.
    K=1 riproduce esattamente jobs/backtest_run (aggiornamento a ogni barra)."""
    entry = d["oa"][j] if direction == "long" else d["ob"][j]
    reward = rr * r_dist
    t = d["ts"][j - 1]
    horizon = t + timedelta(days=MAX_HOLD_DAYS)
    on_pct = OVERNIGHT_PCT.get(epic, OVERNIGHT_DEFAULT)
    peak_fav, peak_r = 0.0, 0.0
    off = OFFSET_FN(0.0, None, rr)      # offset iniziale (stop di partenza)
    exit_r, exit_ts = None, None
    for k in range(j, d["n"]):
        if d["ts"][k] > horizon:
            px = d["ca"][k - 1] if direction == "short" else d["cb"][k - 1]
            exit_r = ((entry - px) if direction == "short" else (px - entry)) / r_dist
            exit_ts = d["ts"][k - 1]
            break
        # il trailing gira ogni K barre: ricalcola l'offset col peak accumulato
        if (k - j) % K == 0:
            frac_tp = peak_fav / reward if reward else None
            off = OFFSET_FN(peak_r, frac_tp, rr)
        if direction == "short":
            sl_price = entry - off * r_dist
            tp_price = entry - reward
            if d["ha"][k] >= sl_price:
                exit_r, exit_ts = off, d["ts"][k]; break
            if d["la"][k] <= tp_price:
                exit_r, exit_ts = rr, d["ts"][k]; break
            fav = entry - d["la"][k]
        else:
            sl_price = entry + off * r_dist
            tp_price = entry + reward
            if d["lb"][k] <= sl_price:
                exit_r, exit_ts = off, d["ts"][k]; break
            if d["hb"][k] >= tp_price:
                exit_r, exit_ts = rr, d["ts"][k]; break
            fav = d["hb"][k] - entry
        if fav > peak_fav:
            peak_fav = fav; peak_r = fav / r_dist
    if exit_r is None:
        return None, None
    nights = (exit_ts.date() - t.date()).days
    fee_r = nights * (on_pct / 100.0) * entry / r_dist
    return exit_r - fee_r, exit_ts


def build_entries(data, ref):
    """Genera gli entry v1-momentum (D=1) con dedup 24h su uscita di riferimento
    K=1. Ritorna lista di (epic, j, direction, r_dist). Stessa selezione per tutti
    i K: cosi' il confronto e' pareggiato sugli stessi trade."""
    idx = {e: {data[e]["ts"][i]: i for i in range(data[e]["n"])} for e in EPICS}
    all_ts = sorted(set().union(*[set(data[e]["ts"]) for e in EPICS]))
    dir_busy = {}
    entries = []
    for t in all_ts:
        best = None
        for e in EPICS:
            i = idx[e].get(t)
            if i is None or data[e]["atr"][i] is None:
                continue
            r0 = ref[e][i]
            if not r0:
                continue
            mc = data[e]["mc"][i]
            dpc = (mc - r0) / r0 * 100.0
            cb, ca = data[e]["cb"][i], data[e]["ca"][i]
            mid = (cb + ca) / 2
            spread = (ca - cb) / mid * 100.0 if mid else 99.0
            if spread > MAX_SPREAD_PCT or abs(dpc) < MIN_ABS_DAILY_PCT:
                continue
            if best is None or abs(dpc) > abs(best[1]):
                best = (e, dpc, i)
        if best is None:
            continue
        e, dpc, i = best
        direction = "long" if dpc > 0 else "short"
        if dir_busy.get((e, direction)) and t < dir_busy[(e, direction)]:
            continue
        j = i + 1
        if j >= data[e]["n"]:
            continue
        atrp = data[e]["atr"][i] / data[e]["mc"][i] * 100.0
        stop_pct = max(STOP_ATR_MULT * atrp, MIN_STOP_PCT)
        r_dist = data[e]["mc"][i] * stop_pct / 100.0
        # uscita di riferimento (K=1) solo per fissare il dedup
        _, exit_ts = simulate_exit_k(data[e], e, j, direction, r_dist, TARGET_RR, 1)
        if exit_ts is None:
            continue
        entries.append((e, j, direction, r_dist))
        dir_busy[(e, direction)] = exit_ts + timedelta(hours=COOLDOWN_H)
    return entries


def main() -> int:
    print("=== Effetto frequenza trailing sulle uscite (v1-momentum, orario) ===")
    data = {e: load(e) for e in EPICS}
    ref = {e: _prev_day_close(data[e]) for e in EPICS}
    entries = build_entries(data, ref)
    print(f"entry (stessi per tutti i K): {len(entries)} | K = {KS}\n")

    res = {}
    for K in KS:
        rs = []
        for e, j, direction, r_dist in entries:
            r_net, _ = simulate_exit_k(data[e], e, j, direction, r_dist, TARGET_RR, K)
            if r_net is not None:
                rs.append(r_net)
        exp = statistics.mean(rs)
        res[K] = rs
        win = sum(1 for r in rs if r > 0) / len(rs)
        print(f"  K={K} (trailing ogni {K}h): n={len(rs):>4} exp_R {exp:+.4f} "
              f"win {win:.0%} mediana {statistics.median(rs):+.3f}")

    e1 = statistics.mean(res[1]); e4 = statistics.mean(res[4])
    diff = e1 - e4
    exps = [statistics.mean(res[K]) for K in (1, 2, 4)]
    monot = exps[0] >= exps[1] >= exps[2]
    r1 = sorted(res[1], reverse=True)
    e1_drop2 = statistics.mean(r1[2:]) if len(r1) > 2 else float("nan")
    robust = (e1_drop2 - e4) >= GATE["robust"]

    print(f"\nexp(K=1) - exp(K=4) = {diff:+.4f}R | monotono K1>=K2>=K4: {monot}")
    print(f"robustezza: exp(K=1) senza top2 {e1_drop2:+.4f} - exp(K=4) {e4:+.4f} "
          f"= {e1_drop2 - e4:+.4f}R (soglia +{GATE['robust']})")

    if diff >= GATE["edge"] and monot and robust:
        v = "FREQUENZA CONTA -> procedi a FASE 2 (candle fini, 5min vs 1min)"
    elif abs(diff) < GATE["flat"]:
        v = "FREQUENZA IRRILEVANTE -> i 5 min attuali bastano, nessuna modifica"
    else:
        v = "INDECISO -> decide l'utente"
    print(f"\n>>> VERDETTO: {v} <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
