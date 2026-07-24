"""Esplorativo — expectancy del momentum v1 PER-ASSET su tutti gli asset con dati.
Aiuta a decidere il paniere (allargare/restringere) sui dati, non a occhio.

Ogni asset valutato da solo: entra quando |dpc|>=0.5% (spread<=0.5%), dedup 24h
per direzione, uscita col motore live (D+V1+V2). Metrica: expectancy_R, IS/OOS.
NB esplorativo, non un gate di deploy: selezionare asset per exp storica e'
ottimizzazione; per un deploy serve pre-registrare IS->scelta, OOS->giudizio.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/basket_asset_scan.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

from jobs.backtest_run import load, OVERNIGHT_PCT, OVERNIGHT_DEFAULT
from jobs.monitor_close_replay import make_offset_fn
from jobs.scan_frequency_backtest import (
    _prev_day_close, MAX_SPREAD_PCT, MIN_ABS_DAILY_PCT,
    STOP_ATR_MULT, MIN_STOP_PCT, TARGET_RR, COOLDOWN_H, MAX_HOLD_DAYS,
)

OFFSET = make_offset_fn(True, True)
IS_END = datetime(2024, 1, 1)
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "candles"

LIVE = {"GOLD", "OIL_BRENT", "US500", "US100", "BTCUSD",
        "COPPER", "HK50", "J225", "EURUSD", "AUDUSD", "GBPUSD"}


def all_epics():
    return sorted({p.name.replace("_HOUR.csv.gz", "")
                   for p in DATA_DIR.glob("*_HOUR.csv.gz")})


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


def per_asset(epic):
    try:
        d = load(epic)
    except Exception:
        return None
    ref = _prev_day_close(d)
    dir_busy = {}
    rs_is, rs_oos = [], []
    for i in range(d["n"]):
        if d["atr"][i] is None or not ref[i]:
            continue
        dpc = (d["mc"][i] - ref[i]) / ref[i] * 100.0
        cb, ca = d["cb"][i], d["ca"][i]
        mid = (cb + ca) / 2
        spread = (ca - cb) / mid * 100.0 if mid else 99.0
        if spread > MAX_SPREAD_PCT or abs(dpc) < MIN_ABS_DAILY_PCT:
            continue
        direction = "long" if dpc > 0 else "short"
        t = d["ts"][i]
        if dir_busy.get(direction) and t < dir_busy[direction]:
            continue
        j = i + 1
        if j >= d["n"]:
            continue
        atrp = d["atr"][i] / d["mc"][i] * 100.0
        r_dist = d["mc"][i] * max(STOP_ATR_MULT * atrp, MIN_STOP_PCT) / 100.0
        r_net, exit_ts = simulate(d, epic, j, direction, r_dist, TARGET_RR)
        if r_net is None:
            continue
        (rs_is if t < IS_END else rs_oos).append(r_net)
        dir_busy[direction] = exit_ts + timedelta(hours=COOLDOWN_H)
    return rs_is, rs_oos


def main() -> int:
    print("=== Expectancy momentum PER-ASSET (uscita live D+V1+V2) ===")
    print(f"{'asset':<12} {'live?':<5} {'n':>4} {'exp_R':>8} {'IS':>8} {'OOS':>8} {'win%':>5}")
    print("-" * 56)
    results = []
    for epic in all_epics():
        r = per_asset(epic)
        if not r:
            continue
        rs_is, rs_oos = r
        allr = rs_is + rs_oos
        if len(allr) < 30:
            continue
        exp = statistics.mean(allr)
        eis = statistics.mean(rs_is) if rs_is else float("nan")
        eoos = statistics.mean(rs_oos) if rs_oos else float("nan")
        win = 100 * sum(1 for x in allr if x > 0) / len(allr)
        results.append((epic, exp, eis, eoos, len(allr), win))
    for epic, exp, eis, eoos, n, win in sorted(results, key=lambda x: -x[1]):
        live = "LIVE" if epic in LIVE else ""
        print(f"{epic:<12} {live:<5} {n:>4} {exp:>+8.3f} {eis:>+8.3f} {eoos:>+8.3f} {win:>4.0f}%")

    # sintesi: paniere live vs candidati
    live_r = [r for r in results if r[0] in LIVE]
    cand_r = [r for r in results if r[0] not in LIVE]
    print(f"\nLIVE ({len(live_r)}): exp media {statistics.mean(x[1] for x in live_r):+.3f} | "
          f"positivi {sum(1 for x in live_r if x[1] > 0)}/{len(live_r)}")
    print(f"CANDIDATI ({len(cand_r)}): exp media {statistics.mean(x[1] for x in cand_r):+.3f} | "
          f"positivi {sum(1 for x in cand_r if x[1] > 0)}/{len(cand_r)}")
    pos_oos = [x for x in results if x[3] > 0 and x[1] > 0]
    print(f"\nAsset con exp>0 SIA totale SIA OOS (candidati robusti): "
          f"{sorted(x[0] for x in pos_oos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
