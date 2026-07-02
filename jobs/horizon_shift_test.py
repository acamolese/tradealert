"""Test cambio orizzonte: Faber GTAA + momentum 12-1 mensili (Sprint 7).

Protocollo e caveat pre-registrati in docs/sprint7-invest-horizon.md.
Parametri canonici dalla letteratura, nessuna ricerca. Un run solo.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/horizon_shift_test.py
"""

from __future__ import annotations

import csv
import gzip
import statistics
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "candles"
UNIVERSE = ["US500", "US100", "US30", "DE40", "UK100", "J225", "AU200",
            "GOLD", "SILVER", "COPPER", "OIL_BRENT", "NATURALGAS",
            "BTCUSD", "ETHUSD"]
SMA_MONTHS = 10
MOM_LOOK, MOM_SKIP, MOM_TOP = 12, 1, 3
COST = 0.001  # 0.10% per cambio stato sleeve


def monthly_closes(epic):
    """Ultimo mid-close di ogni mese di calendario."""
    out = {}
    with gzip.open(DATA_DIR / f"{epic}_DAY.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            try:
                mid = (float(r["close_bid"]) + float(r["close_ask"])) / 2
            except (ValueError, TypeError):
                continue
            out[r["ts"][:7]] = mid  # sovrascrive: resta l'ultimo del mese
    return out


def stats(rets, months, label):
    if not rets:
        return
    eq, peak, maxdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        maxdd = max(maxdd, 1 - eq / peak)
    years = len(rets) / 12
    cagr = eq ** (1 / years) - 1
    mu, sd = statistics.mean(rets), statistics.pstdev(rets)
    sharpe = (mu / sd * 12 ** 0.5) if sd else float("nan")
    # anno peggiore
    by_year = {}
    for m, r in zip(months, rets):
        by_year.setdefault(m[:4], []).append(r)
    yr_rets = {y: (lambda rs: __import__("math").prod(1 + x for x in rs) - 1)(rs)
               for y, rs in by_year.items()}
    worst_y = min(yr_rets, key=yr_rets.get)
    half = len(rets) // 2
    eq1 = __import__("math").prod(1 + r for r in rets[:half]) - 1
    eq2 = __import__("math").prod(1 + r for r in rets[half:]) - 1
    print(f"{label:<22} CAGR {cagr:+6.1%}  maxDD {maxdd:5.1%}  Sharpe {sharpe:4.2f}  "
          f"anno peggiore {worst_y} {yr_rets[worst_y]:+.1%}  "
          f"1a metà {eq1:+.1%} | 2a metà {eq2:+.1%}")
    return cagr, maxdd, sharpe, eq1, eq2


def main() -> int:
    global UNIVERSE
    if "--exclude" in sys.argv:
        excl = set(sys.argv[sys.argv.index("--exclude") + 1].split(","))
        UNIVERSE = [e for e in UNIVERSE if e not in excl]
    data = {e: monthly_closes(e) for e in UNIVERSE}
    months = sorted(set().union(*[set(d) for d in data.values()]))
    # serie di rendimenti mensili per asset (None se dati assenti)
    rets = {e: {} for e in UNIVERSE}
    for e in UNIVERSE:
        ms = sorted(data[e])
        for a, b in zip(ms, ms[1:]):
            rets[e][b] = data[e][b] / data[e][a] - 1
    for e in UNIVERSE:
        ms = sorted(data[e])
        print(f"  {e:<11} dati da {ms[0]} a {ms[-1]} ({len(ms)} mesi)")

    bh, fab, mom = [], [], []
    out_months = []
    held_fab = {e: False for e in UNIVERSE}
    held_mom: set[str] = set()
    for i, m in enumerate(months):
        if i < MOM_LOOK + 1:
            continue
        prev = months[i - 1]
        # rendimento del mese m per ogni asset disponibile
        avail = [e for e in UNIVERSE if m in rets[e]]
        if len(avail) < 5:
            continue
        out_months.append(m)
        # --- buy & hold equal weight ---
        bh.append(statistics.mean(rets[e][m] for e in avail))
        # --- Faber: segnale calcolato a fine mese PRECEDENTE ---
        r_f, switches = [], 0
        for e in avail:
            ms_e = [x for x in months[:i] if x in data[e]]
            if len(ms_e) < SMA_MONTHS:
                r_f.append(0.0)
                continue
            sma = statistics.mean(data[e][x] for x in ms_e[-SMA_MONTHS:])
            sig = data[e][prev] > sma if prev in data[e] else False
            if sig != held_fab[e]:
                switches += 1
                held_fab[e] = sig
            r_f.append(rets[e][m] if sig else 0.0)
        fab.append(statistics.mean(r_f) - switches * COST / len(avail))
        # --- momentum 12-1: rank a fine mese precedente ---
        scores = {}
        for e in avail:
            m_start = months[i - 1 - MOM_LOOK]
            m_end = months[i - 1 - MOM_SKIP]
            if m_start in data[e] and m_end in data[e]:
                scores[e] = data[e][m_end] / data[e][m_start] - 1
        top = [e for e in sorted(scores, key=scores.get, reverse=True)[:MOM_TOP]
               if scores[e] > 0]
        switches = len(set(top) ^ held_mom)
        held_mom = set(top)
        r_m = statistics.mean(rets[e][m] for e in top) if top else 0.0
        mom.append(r_m - switches * COST / max(len(top), 1) / MOM_TOP)

    print(f"\nperiodo: {out_months[0]} -> {out_months[-1]} ({len(out_months)} mesi)\n")
    stats(bh, out_months, "Buy&Hold EW")
    stats(fab, out_months, "Faber SMA10m")
    stats(mom, out_months, "Momentum 12-1 top3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
