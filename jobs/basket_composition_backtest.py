"""Sprint 8 — Test composizione paniere x soglia d'ingresso (cross-asset).
Esplorativo/validazione dell'idea: allargare il paniere + alzare la soglia.

A ogni tick il selettore v1-momentum prende il TOP mover del SET dato con
score>=soglia (score=7.0+min(|dpc|/10,0.9) -> soglia 7.2 = |dpc|>=2%), dedup 24h,
uscita col motore live (D+V1+V2). Confronta panieri e soglie su exp_R e n trade.

Caveat: bracket+trailing, NON il monitor LLM (non simulabile). Confronto RELATIVO
tra panieri valido; il livello assoluto sottostima l'esito reale (monitor aggiunge).

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/basket_composition_backtest.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta

from jobs.backtest_run import load, OVERNIGHT_PCT, OVERNIGHT_DEFAULT
from jobs.monitor_close_replay import make_offset_fn
from jobs.scan_frequency_backtest import (
    _prev_day_close, MAX_SPREAD_PCT, STOP_ATR_MULT, MIN_STOP_PCT,
    TARGET_RR, COOLDOWN_H, MAX_HOLD_DAYS,
)

OFFSET = make_offset_fn(True, True)
IS_END = datetime(2024, 1, 1)

ATTUALE_11 = ["GOLD", "OIL_BRENT", "US500", "US100", "BTCUSD",
              "COPPER", "HK50", "J225", "EURUSD", "AUDUSD", "GBPUSD"]
PLUS_INDICI = ATTUALE_11 + ["DE40", "US30"]
SOLO_INDICI = ["US500", "US100", "DE40", "US30", "GOLD"]
BASKETS = {"attuale_11": ATTUALE_11, "+indici_13": PLUS_INDICI, "solo_indici_5": SOLO_INDICI}
SOGLIE = {"7.0": 0.5, "7.2": 2.0}   # soglia score -> |dpc| minimo


def simulate(d, epic, j, direction, r_dist, rr):
    entry = d["oa"][j] if direction == "long" else d["ob"][j]
    reward = rr * r_dist
    t = d["ts"][j - 1]
    horizon = t + timedelta(days=MAX_HOLD_DAYS)
    on_pct = OVERNIGHT_PCT.get(epic, OVERNIGHT_DEFAULT)
    peak_fav, peak_r = 0.0, 0.0
    exit_r, exit_ts = None, None
    for k in range(j, d["n"]):
        if d["ts"][k] > horizon:
            px = d["ca"][k - 1] if direction == "short" else d["cb"][k - 1]
            exit_r = ((entry - px) if direction == "short" else (px - entry)) / r_dist
            exit_ts = d["ts"][k - 1]; break
        off = OFFSET(peak_r, (peak_fav / reward if reward else None), rr)
        if direction == "short":
            if d["ha"][k] >= entry - off * r_dist:
                exit_r, exit_ts = off, d["ts"][k]; break
            if d["la"][k] <= entry - reward:
                exit_r, exit_ts = rr, d["ts"][k]; break
            fav = entry - d["la"][k]
        else:
            if d["lb"][k] <= entry + off * r_dist:
                exit_r, exit_ts = off, d["ts"][k]; break
            if d["hb"][k] >= entry + reward:
                exit_r, exit_ts = rr, d["ts"][k]; break
            fav = d["hb"][k] - entry
        if fav > peak_fav:
            peak_fav = fav; peak_r = fav / r_dist
    if exit_r is None:
        return None, None
    nights = (exit_ts.date() - t.date()).days
    return exit_r - nights * (on_pct / 100.0) * entry / r_dist, exit_ts


def run_basket(data, ref, idx, epics, min_dpc):
    all_ts = sorted(set().union(*[set(data[e]["ts"]) for e in epics]))
    dir_busy = {}
    rs_is, rs_oos = [], []
    for t in all_ts:
        best = None
        for e in epics:
            i = idx[e].get(t)
            if i is None or data[e]["atr"][i] is None or not ref[e][i]:
                continue
            dpc = (data[e]["mc"][i] - ref[e][i]) / ref[e][i] * 100.0
            cb, ca = data[e]["cb"][i], data[e]["ca"][i]
            mid = (cb + ca) / 2
            spread = (ca - cb) / mid * 100.0 if mid else 99.0
            if spread > MAX_SPREAD_PCT or abs(dpc) < min_dpc:
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
        r_dist = data[e]["mc"][i] * max(STOP_ATR_MULT * atrp, MIN_STOP_PCT) / 100.0
        r_net, exit_ts = simulate(data[e], e, j, direction, r_dist, TARGET_RR)
        if r_net is None:
            continue
        (rs_is if t < IS_END else rs_oos).append(r_net)
        dir_busy[(e, direction)] = exit_ts + timedelta(hours=COOLDOWN_H)
    return rs_is, rs_oos


def main() -> int:
    epics = sorted(set(sum(BASKETS.values(), [])))
    data = {e: load(e) for e in epics}
    ref = {e: _prev_day_close(data[e]) for e in epics}
    idx = {e: {data[e]["ts"][i]: i for i in range(data[e]["n"])} for e in epics}

    print("=== Composizione paniere x soglia (uscita live, NO monitor) ===")
    print(f"{'paniere':<15} {'soglia':<7} {'n':>5} {'exp_R':>8} {'IS':>8} {'OOS':>8} {'somma':>8}")
    print("-" * 62)
    for bname, blist in BASKETS.items():
        for sname, mindpc in SOGLIE.items():
            rs_is, rs_oos = run_basket(data, ref, idx, blist, mindpc)
            allr = rs_is + rs_oos
            if not allr:
                continue
            exp = statistics.mean(allr)
            eis = statistics.mean(rs_is) if rs_is else float("nan")
            eoos = statistics.mean(rs_oos) if rs_oos else float("nan")
            print(f"{bname:<15} {sname:<7} {len(allr):>5} {exp:>+8.3f} "
                  f"{eis:>+8.3f} {eoos:>+8.3f} {sum(allr):>+8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
