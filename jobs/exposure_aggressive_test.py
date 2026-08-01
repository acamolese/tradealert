"""Evolutiva v2 "piu' aggressivo" — confronto config su US500 daily. Sola lettura.

Contesto (2026-08-01): con BLOCK_MARGIN=20, L=20, GAP_TOLERANCE=0.10 il primo
blocco richiede equity >= 60€. Dopo la prima perdita reale (equity 55.63€) N_max
e' 0 in permanenza: STALLO STRUTTURALE, il sistema flat non puo' piu' rientrare.

Simula la meccanica REALE a blocchi del controller (N_max da equity dinamica,
isteresi 2gg sugli aumenti, riduzioni immediate, kill floor 40€) su tutto lo
storico, partendo da 60€. Config a confronto:
  A attuale     m=20 st=0.15 ms=2.0 g=0.10   (blocco 400€, soglia 60€)
  B granulare   m=10 st=0.15 ms=2.0 g=0.10   (blocco 200€, soglia 30€)
  C aggressiva  m=10 st=0.20 ms=3.0 g=0.05   (blocco 200€, soglia 20€)
  D spinta      m=10 st=0.25 ms=3.0 g=0.05
  BH benchmark  sempre 1 blocco 400€ (equivalente all'always_1_block)

Stessi costi del harness sprint 9 (FIN_DAILY su esposizione, SPREAD su turnover).
Metriche: ritorno totale, CAGR, maxDD, % giorni investito, mosse, giorni in
stallo (N_max=0), esposizione media.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/exposure_aggressive_test.py
"""
from __future__ import annotations

import math
import sys

from jobs.profit_lock_test import load_closes, rolling_sigma, FIN_DAILY, SPREAD

LEV = 20.0
START_EQ = 60.0
KILL_FLOOR = 40.0
HYST_DAYS = 2


def simulate_blocks(closes, sig, m, st, ms, g):
    """Replica plan_exposure a blocchi con equity dinamica. Ritorna metriche."""
    notional = m * LEV
    eq = START_EQ
    blocks = 0
    prev_targets: list[int] = []
    days = invested = stalled = moves = 0
    exp_sum = 0.0
    peak, maxdd = eq, 0.0
    halted = False
    for i in range(1, len(closes)):
        s = sig[i - 1]              # sigma nota a fine giorno precedente
        ret = math.log(closes[i] / closes[i - 1])
        # pnl del giorno sull'esposizione tenuta
        if blocks > 0:
            eq += blocks * notional * (math.exp(ret) - 1.0) - blocks * notional * FIN_DAILY
        days += 1
        invested += 1 if blocks > 0 else 0
        exp_sum += blocks * notional
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak if peak > 0 else 0.0)
        # --- decisione di fine giornata (come il cron 23:30) ---
        if halted:
            continue
        if eq < KILL_FLOOR:
            if blocks:
                eq -= blocks * notional * SPREAD
                moves += blocks
                blocks = 0
            halted = True
            continue
        n_max = max(0, int(eq // (m * (1.0 + LEV * g))))
        if n_max == 0:
            stalled += 1
        if s is None:
            tgt_raw = 0
        else:
            scale = min(max(st / s, 0.0), ms)
            tgt_raw = int(round(scale))
        tgt = min(tgt_raw, n_max)
        if tgt < blocks:                      # riduzioni immediate (anche n_max)
            eq -= (blocks - tgt) * notional * SPREAD
            moves += blocks - tgt
            blocks = tgt
        elif tgt > blocks:                    # aumenti con isteresi
            tail = prev_targets[-(HYST_DAYS - 1):] if HYST_DAYS > 1 else []
            if len(tail) >= HYST_DAYS - 1 and all(t == tgt for t in tail):
                eq -= (tgt - blocks) * notional * SPREAD
                moves += tgt - blocks
                blocks = tgt
        prev_targets.append(tgt)
    years = days / 252.0
    cagr = (eq / START_EQ) ** (1 / years) - 1 if years > 0 and eq > 0 else -1.0
    return {
        "eq_fin": eq, "tot": eq / START_EQ - 1.0, "cagr": cagr, "maxdd": maxdd,
        "pct_inv": invested / days if days else 0.0, "moves": moves,
        "stalled": stalled, "exp_media": exp_sum / days if days else 0.0,
        "halted": halted,
    }


def main() -> int:
    global START_EQ, KILL_FLOOR
    closes = load_closes()
    sig = rolling_sigma(closes)
    print(f"barre US500 daily: {len(closes)} (~{len(closes)/252:.1f} anni)\n")
    configs = [
        ("A attuale   m20 st.15 ms2 g.10", 20.0, 0.15, 2.0, 0.10),
        ("B granulare m10 st.15 ms2 g.10", 10.0, 0.15, 2.0, 0.10),
        ("C aggress.  m10 st.20 ms3 g.05", 10.0, 0.20, 3.0, 0.05),
        ("D spinta    m10 st.25 ms3 g.05", 10.0, 0.25, 3.0, 0.05),
        ("E micro     m5  st.15 ms2 g.10", 5.0, 0.15, 2.0, 0.10),
        ("F micro agg m5  st.20 ms3 g.10", 5.0, 0.20, 3.0, 0.10),
        ("G micro max m5  st.25 ms3 g.05", 5.0, 0.25, 3.0, 0.05),
    ]
    import itertools
    for start, floor in itertools.product((55.63,), (40.0, 25.0, 20.0)):
        START_EQ, KILL_FLOOR = start, floor
        print(f"--- start equity {start:.2f}€ | kill floor {floor:.0f}€ ---")
        print(f"{'config':<34} {'eq fin':>8} {'tot':>8} {'CAGR':>7} {'maxDD':>7} "
              f"{'%inv':>6} {'mosse':>6} {'stallo':>7} {'exp med':>8}")
        for name, m, st, ms, g in configs:
            r = simulate_blocks(closes, sig, m, st, ms, g)
            halt = " HALT" if r["halted"] else ""
            print(f"{name:<34} {r['eq_fin']:>7.1f}€ {r['tot']*100:>+7.1f}% "
                  f"{r['cagr']*100:>+6.2f}% {r['maxdd']*100:>6.1f}% "
                  f"{r['pct_inv']*100:>5.0f}% {r['moves']:>6d} {r['stalled']:>6d}g "
                  f"{r['exp_media']:>7.0f}€{halt}")
        print()
    START_EQ = 60.0
    # benchmark: sempre 1 blocco 400€ (mai flat, nessun targeting)
    eq, peak, maxdd = START_EQ, START_EQ, 0.0
    for i in range(1, len(closes)):
        ret = math.log(closes[i] / closes[i - 1])
        eq += 400.0 * (math.exp(ret) - 1.0) - 400.0 * FIN_DAILY
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak if peak > 0 else 0.0)
    years = (len(closes) - 1) / 252.0
    cagr = (eq / START_EQ) ** (1 / years) - 1 if eq > 0 else -1.0
    print(f"{'BH sempre 1 blocco 400€':<34} {eq:>7.1f}€ {(eq/START_EQ-1)*100:>+7.1f}% "
          f"{cagr*100:>+6.2f}% {maxdd*100:>6.1f}% {'100%':>6} {'-':>6} {'-':>7} "
          f"{400:>7.0f}€")
    return 0


if __name__ == "__main__":
    sys.exit(main())
