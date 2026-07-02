"""Gate direzionale lato long (Sprint 6 A2, gate pre-registrato).

Campione: trade chiusi dal 2026-05-20 (fix bidirezionale live), normalizzati in R.
Controfattuale: per i trade chiusi dal monitor usa exit_R della simulazione
bracket-only di A1 (docs/sprint6-monitor-replay.csv); per gli altri (stop/tp/
trailing) il bracket ha gia' deciso, quindi controfattuale = reale.

Gate (docs/sprint6-piano-scalata.md):
- LONG DA BLOCCARE: expectancy_R long <= -0.10R sia reale SIA controfattuale,
  n>=30, robusto a (a) senza i 2 long peggiori e (b) solo 5 asset core.
- PROBLEMA DI GESTIONE: reale <= -0.10R ma controfattuale >= 0.
- Altrimenti NON CONCLUSIVO (ripetere a +15 long chiusi).

Sola lettura. Uso: PYTHONPATH=$PWD .venv/bin/python jobs/long_gate_analysis.py
"""

from __future__ import annotations

import csv
import statistics
import sys

from src.config import load_config
from src.db import Database

CUTOFF = "2026-05-20"
CORE = {"Gold", "Brent Oil", "US500", "Nasdaq 100", "Bitcoin"}
CF_CSV = "docs/sprint6-monitor-replay.csv"


def main() -> int:
    cfg = load_config()
    db = Database(cfg)

    cf_map = {}
    with open(CF_CSV) as f:
        for row in csv.DictReader(f):
            cf_map[int(row["id"])] = float(row["cf_r"])

    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").not_.is_("pnl", "null")
              .gte("closed_at", CUTOFF).order("id").execute().data)

    rows, skipped = [], []
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        sl_pct = sig.get("stop_loss") if sig else None
        if not sl_pct:
            skipped.append((t["id"], "manca signal/sl"))
            continue
        entry = float(t["entry_price"])
        r_dist = entry * float(sl_pct) / 100.0
        if t.get("close_price") is not None:
            cp = float(t["close_price"])
            real_r = ((entry - cp) if t["direction"] == "short" else (cp - entry)) / r_dist
        else:
            real_r = float(t["pnl"]) / (r_dist * float(t["size"]))
        cf_r = cf_map.get(t["id"], real_r)  # non-monitor: bracket = reale
        rows.append({"id": t["id"], "asset": t["asset"], "dir": t["direction"],
                     "core": t["asset"] in CORE, "real_r": real_r, "cf_r": cf_r,
                     "monitor_closed": t["id"] in cf_map})

    print(f"trade chiusi dal {CUTOFF}: {len(trades)} | analizzati: {len(rows)} | scartati: {len(skipped)}")
    for sid, why in skipped:
        print(f"  scartato #{sid}: {why}")

    def exp(subset, key):
        vals = [r[key] for r in subset]
        return statistics.mean(vals) if vals else float("nan"), len(vals)

    def report(label, subset):
        er, n = exp(subset, "real_r")
        ec, _ = exp(subset, "cf_r")
        print(f"{label:<38} n={n:>3} exp_R reale {er:+.3f} | controfattuale {ec:+.3f}")
        return er, ec, n

    print("\n=== Expectancy in R (reale vs controfattuale bracket-only) ===")
    longs = [r for r in rows if r["dir"] == "long"]
    shorts = [r for r in rows if r["dir"] == "short"]
    er_l, ec_l, n_l = report("LONG", longs)
    report("SHORT", shorts)
    srt = sorted(longs, key=lambda r: r["real_r"])
    er_l_rob, ec_l_rob, _ = report("LONG senza i 2 peggiori", srt[2:])
    er_l_core, ec_l_core, n_core = report("LONG solo core", [r for r in longs if r["core"]])

    print("\n=== LONG per asset ===")
    assets = sorted({r["asset"] for r in longs})
    for a in assets:
        sub = [r for r in longs if r["asset"] == a]
        er, n = exp(sub, "real_r")
        print(f"  {a:<14} n={n:>2} exp_R reale {er:+.3f}")

    block = (er_l <= -0.10 and ec_l <= -0.10 and n_l >= 30
             and er_l_rob <= -0.10 and ec_l_rob <= -0.10
             and er_l_core <= -0.10 and ec_l_core <= -0.10)
    gestione = (er_l <= -0.10 and ec_l >= 0)
    if block:
        verdict = "LONG DA BLOCCARE (rischio dimezzato LONG_RISK_FACTOR=0.5)"
    elif gestione:
        verdict = "PROBLEMA DI GESTIONE, NON DI SELEZIONE"
    else:
        verdict = "NON CONCLUSIVO (ripetere a +15 long chiusi)"
    print(f"\n>>> VERDETTO: {verdict} <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
