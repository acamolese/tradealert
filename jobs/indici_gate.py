"""Gate forward del paniere INDICI (Sprint 8, pre-registrato in
docs/sprint8-indici-gate.md). Sola lettura.

Confronta l'expectancy_R dei trade chiusi dal ridisegno (2026-07-25, tutti su
asset del paniere indici) con le soglie pre-registrate. Lavora in R (i pnl euro
del DB sono inaffidabili).

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/indici_gate.py
"""
from __future__ import annotations

import statistics
import sys

from src.config import load_config
from src.db import Database

START = "2026-07-25"
N_TARGET = 25
CONFIRM = 0.05
ROLLBACK = -0.05
BASELINE_MISTO = 0.030
INDEX_NAMES = {"US500", "Nasdaq 100", "Gold", "Germany 40", "Wall Street 30"}


def real_r(t, sig):
    sl = sig.get("stop_loss") if sig else None
    if not sl or t.get("close_price") is None:
        return None
    entry = float(t["entry_price"])
    cp = float(t["close_price"])
    rd = entry * float(sl) / 100.0
    if rd <= 0:
        return None
    return ((entry - cp) if t["direction"] == "short" else (cp - entry)) / rd


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    tr = (db._client.table("trades").select("*")
          .eq("status", "closed").not_.is_("close_price", "null")
          .gte("closed_at", START).order("closed_at").execute().data)
    rows = []
    for t in tr:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        r = real_r(t, sig)
        if r is not None:
            rows.append({"id": t["id"], "asset": t["asset"], "dir": t["direction"],
                         "r": r, "exit": t.get("exit_reason"), "closed": t["closed_at"]})

    rs = [x["r"] for x in rows]
    n = len(rs)
    print(f"=== GATE FORWARD PANIERE INDICI (dal {START}) ===")
    print(f"trade indici chiusi: {n} / target {N_TARGET}")
    # sanity: eventuali asset fuori paniere (non dovrebbero esserci)
    fuori = {x["asset"] for x in rows if x["asset"] not in INDEX_NAMES}
    if fuori:
        print(f"  ATTENZIONE: trade su asset NON-indice dopo il ridisegno: {fuori}")

    if n == 0:
        print("Nessun trade indici ancora chiuso. Ripassare piu' avanti.")
        return 0

    exp = statistics.mean(rs)
    win = 100 * sum(1 for r in rs if r > 0) / n
    print(f"exp_R_fwd: {exp:+.3f} | win {win:.0f}% | mediana {statistics.median(rs):+.3f} "
          f"| somma {sum(rs):+.2f}")
    print(f"baseline misto pre-ridisegno: +{BASELINE_MISTO:.3f} | backtest indici: +0.052")
    if n > 2:
        srt = sorted(rs, reverse=True)
        print(f"robustezza (senza 2 migliori): {statistics.mean(srt[2:]):+.3f}")

    print("\nper asset:")
    byasset = {}
    for x in rows:
        byasset.setdefault(x["asset"], []).append(x["r"])
    for a in sorted(byasset, key=lambda a: -statistics.mean(byasset[a])):
        v = byasset[a]
        print(f"  {a:<16} n={len(v):>2} exp_R {statistics.mean(v):+.3f}")

    print("\ndettaglio:")
    for x in rows:
        print(f"  #{x['id']:>4} {x['asset']:<16} {x['dir']:<5} R={x['r']:+.2f}  "
              f"{(x.get('exit') or '')[:20]:<20} {x['closed'][:16]}")

    # verdetto
    print("\n=== VERDETTO ===")
    if n < N_TARGET:
        print(f">>> CAMPIONE INSUFFICIENTE (n={n} < {N_TARGET}): nessun giudizio, continuare <<<")
        return 0
    robust = statistics.mean(sorted(rs, reverse=True)[2:]) if n > 2 else exp
    if exp >= CONFIRM and exp > 0 and robust >= 0:
        v = "RIDISEGNO CONFERMATO (tieni indici; valuta rialzo sizing)"
    elif exp <= ROLLBACK:
        v = "RIDISEGNO FALLITO (rollback paniere o riconsidera Bitcoin)"
    else:
        v = "NON CONCLUSIVO (ripetere a +15 trade, n>=40)"
    print(f">>> {v} <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
