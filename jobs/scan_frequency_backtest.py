"""Sprint 8 Fase 1 — Effetto del ritardo di ingresso sul v1-momentum.
Pre-registrato in docs/sprint8-scan-frequency.md. Sola lettura (candle su disco).

Ricostruisce il selettore deterministico v1-momentum su barre orarie e misura
l'expectancy_R netta al variare del RITARDO di ingresso D (barre tra il close del
segnale e il fill). D=1 = scan orario ideale; D crescente = scan che entra piu'
tardi. Frequenza scan maggiore ⇒ ritardo minore, quindi il confronto exp(D=1) vs
exp(D=3) dice se entrare prima (piu' frequente) migliora l'esito.

Uscita: motore live (SL k·ATR, TP rr·SL, trailing D+V1+V2) riusato da
jobs/backtest_run.py (spread bid/ask, overnight fee, cooldown 24h, max hold 30g).

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/scan_frequency_backtest.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import timedelta

from jobs.backtest_run import load, OFFSET_FN, OVERNIGHT_PCT, OVERNIGHT_DEFAULT

# Paniere live (11 asset). Epic candle == epic Capital.
EPICS = ["GOLD", "OIL_BRENT", "US500", "US100", "BTCUSD",
         "COPPER", "HK50", "J225", "EURUSD", "AUDUSD", "GBPUSD"]

MAX_SPREAD_PCT = 0.5
MIN_ABS_DAILY_PCT = 0.5
STOP_ATR_MULT = 1.5
MIN_STOP_PCT = 0.5
TARGET_RR = 2.0
COOLDOWN_H = 24
MAX_HOLD_DAYS = 30
DELAYS = [1, 2, 3, 4]           # barre orarie di ritardo di ingresso
GATE = {"edge": 0.10, "flat": 0.05, "robust": 0.05}


def _prev_day_close(d):
    """Per ogni barra i, mid_close dell'ultima barra del giorno di calendario
    PRECEDENTE (riferimento per il daily_pct_change, ~percentageChange Capital)."""
    ts, mc = d["ts"], d["mc"]
    ref = [None] * len(ts)
    last_close_of_day = {}
    cur_ref = None
    cur_day = None
    for i in range(len(ts)):
        day = ts[i].date()
        if cur_day is None:
            cur_day = day
        if day != cur_day:
            # nuovo giorno: il ref e' l'ultimo close del giorno precedente
            cur_ref = last_close_of_day.get(cur_day)
            cur_day = day
        ref[i] = cur_ref
        last_close_of_day[day] = mc[i]
    return ref


def simulate_exit(d, epic, j, direction, sl_atr_dist, rr):
    """Simula l'uscita col motore live dalla barra j. Ritorna (r_net, exit_ts)
    o (None, None) se i dati finiscono col trade aperto. Copia fedele della
    logica di jobs/backtest_run.run_config."""
    entry = d["oa"][j] if direction == "long" else d["ob"][j]
    r_dist = sl_atr_dist
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
            exit_ts = d["ts"][k - 1]
            break
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


def run(delay):
    """Simula il v1-momentum cross-asset con ritardo di ingresso `delay` barre.
    A ogni timestamp comune, sceglie il top |dpc| eligibile non in cooldown."""
    data = {e: load(e) for e in EPICS}
    ref = {e: _prev_day_close(data[e]) for e in EPICS}
    # indice per timestamp per ogni asset (per lookup del top a ogni tick)
    idx = {e: {data[e]["ts"][i]: i for i in range(data[e]["n"])} for e in EPICS}
    # griglia di tick = unione ordinata di tutti i timestamp
    all_ts = sorted(set().union(*[set(data[e]["ts"]) for e in EPICS]))

    busy_until = {}   # epic -> ts fine cooldown (dedup per asset+direzione)
    dir_busy = {}     # (epic, direction) -> ts fine cooldown
    trades = []
    for t in all_ts:
        # costruisci i candidati eligibili a questo tick
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
            # spread% dalla barra (ask/bid del close)
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
        # dedup 24h per (asset, direzione)
        if dir_busy.get((e, direction)) and t < dir_busy[(e, direction)]:
            continue
        j = i + delay
        if j >= data[e]["n"] or data[e]["atr"][i] is None:
            continue
        atrp = data[e]["atr"][i] / data[e]["mc"][i] * 100.0
        stop_pct = max(STOP_ATR_MULT * atrp, MIN_STOP_PCT)
        sl_dist = data[e]["mc"][i] * stop_pct / 100.0
        r_net, exit_ts = simulate_exit(data[e], e, j, direction, sl_dist, TARGET_RR)
        if r_net is None:
            continue
        trades.append({"epic": e, "dir": direction, "r_net": r_net,
                       "entry_ts": data[e]["ts"][j], "exit_ts": exit_ts, "dpc": dpc})
        dir_busy[(e, direction)] = exit_ts + timedelta(hours=COOLDOWN_H)
    return trades


def summ(trades):
    rs = [t["r_net"] for t in trades]
    if not rs:
        return {"n": 0, "exp": float("nan")}
    return {"n": len(rs), "exp": statistics.mean(rs),
            "win": sum(1 for r in rs if r > 0) / len(rs),
            "med": statistics.median(rs)}


def main() -> int:
    print("=== FASE 1: expectancy_R vs ritardo di ingresso (v1-momentum, orario) ===")
    print(f"paniere: {len(EPICS)} asset | delay in barre orarie: {DELAYS}\n")
    res = {}
    for D in DELAYS:
        tr = run(D)
        s = summ(tr)
        res[D] = (tr, s)
        print(f"  D={D} (ritardo {D}h): n={s['n']:>4} exp_R {s['exp']:+.4f} "
              f"win {s.get('win',0):.0%} mediana {s.get('med',0):+.3f}")

    e1 = res[1][1]["exp"]; e3 = res[3][1]["exp"]
    diff = e1 - e3
    # monotonia
    exps = [res[D][1]["exp"] for D in (1, 2, 3)]
    monot = exps[0] >= exps[1] >= exps[2]
    # robustezza: exp(1) senza i 2 migliori vs exp(3) pieno
    r1 = sorted((t["r_net"] for t in res[1][0]), reverse=True)
    e1_drop2 = statistics.mean(r1[2:]) if len(r1) > 2 else float("nan")
    robust = (e1_drop2 - e3) >= GATE["robust"]

    print(f"\nexp(D=1) - exp(D=3) = {diff:+.4f}R | monotono D1>=D2>=D3: {monot}")
    print(f"robustezza: exp(D=1) senza top2 {e1_drop2:+.4f} - exp(D=3) {e3:+.4f} "
          f"= {e1_drop2 - e3:+.4f}R (soglia +{GATE['robust']})")

    if diff >= GATE["edge"] and monot and robust:
        v = "TIMING CONTA -> procedi a FASE 2 (dati fini 15/30 min)"
    elif abs(diff) < GATE["flat"]:
        v = "TIMING IRRILEVANTE -> STOP, non toccare il cron (E2 non testato)"
    else:
        v = "INDECISO -> decide l'utente"
    print(f"\n>>> VERDETTO FASE 1: {v} <<<")

    # breakdown per direzione (a D=1) e per asset
    print("\n--- D=1 per direzione ---")
    for dd in ("long", "short"):
        sub = [t for t in res[1][0] if t["dir"] == dd]
        if sub:
            print(f"  {dd:<5} n={len(sub):>3} exp_R {statistics.mean(t['r_net'] for t in sub):+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
