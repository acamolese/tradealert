"""Sprint 8 Fase 1 — Backtest weekend-hold (pre-registrato in
docs/sprint8-weekend-hold-gate.md). Sola lettura.

Parte B del gate: sui trade reali tenuti attraverso un weekend, su asset
gap-prone (no crypto), misura dalle candele HOUR:
  M1 = gap direzionale in R (open_lunedi - close_venerdi)/r_dist * segno(dir).
       Cio' che la policy SALTA-GAP eviterebbe (se <0) o perderebbe (se >0).
  M2 = R_reale - R_al_close_venerdi. Cio' che la policy ESCI-E-BASTA
       rinuncia/evita (gap + traiettoria di lunedi+). E_tieni = media di M2.

La Parte A strutturale (Tail_p90) resta in jobs/weekend_gap_analysis.py.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/weekend_hold_backtest.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient
from src.universe import UNIVERSE
from jobs.weekend_gap_analysis import _fetch_hour, _mid, _spans_weekend, _pctl

# asset gap-prone (mercato che chiude nel weekend); crypto escluse per pre-reg
GAP_PRONE = {"Gold", "Brent Oil", "Hang Seng", "Nasdaq 100", "US500"}
EPIC = {a.name: a.epic for a in UNIVERSE}
WK_MIN_H = 24.0   # un gap "weekend" vero ha un buco >=24h (esclude pause daily)
OUTLIER = 89      # #89 Hang Seng, caso scatenante sprint5, da isolare


def _weekend_gaps(candles, o, c):
    """Coppie (close_ven, open_lun) per ogni boundary weekend dentro [o, c]."""
    out = []
    for a, b in zip(candles, candles[1:]):
        t1 = datetime.fromisoformat(a["snapshotTimeUTC"])
        t2 = datetime.fromisoformat(b["snapshotTimeUTC"])
        dt_h = (t2 - t1).total_seconds() / 3600
        if dt_h < WK_MIN_H or not _spans_weekend(t1, t2):
            continue
        # il gap deve cadere mentre il trade e' aperto
        if t2 < o or t1 > c:
            continue
        cv = _mid(a, "closePrice"); ol = _mid(b, "openPrice")
        if cv > 0:
            out.append((t1, cv, ol))
    return out


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").not_.is_("close_price", "null")
              .order("id").execute().data)
    sig_stop = {s["id"]: s for s in
                db._client.table("signals").select("id,stop_loss").execute().data}

    rows, skipped = [], []
    for t in trades:
        asset = t["asset"]
        if asset not in GAP_PRONE:
            continue
        op = datetime.fromisoformat(t["opened_at"]).replace(tzinfo=None) if t.get("opened_at") else None
        clo = datetime.fromisoformat(t["closed_at"]).replace(tzinfo=None) if t.get("closed_at") else None
        if not (op and clo) or not _spans_weekend(op, clo):
            continue
        sg = sig_stop.get(t.get("signal_id"), {})
        if not sg.get("stop_loss"):
            skipped.append((t["id"], asset, "manca stop_loss"))
            continue
        epic = EPIC.get(asset)
        entry = float(t["entry_price"]); close = float(t["close_price"])
        r_dist = entry * float(sg["stop_loss"]) / 100.0
        d = 1 if t["direction"] == "long" else -1

        candles = _fetch_hour(cl, epic, op - timedelta(hours=6), clo + timedelta(hours=6))
        gaps = _weekend_gaps(candles, op, clo)
        if not gaps:
            skipped.append((t["id"], asset, "candele HOUR non coprono il weekend"))
            continue
        # M1: somma dei gap direzionali su tutti i weekend attraversati
        m1 = sum((ol - cv) / r_dist * d for _, cv, ol in gaps)
        # M2: R reale meno R al primo close venerdi
        cv0 = gaps[0][1]
        r_close_ven = (cv0 - entry) / r_dist * d
        r_real = (close - entry) / r_dist * d
        m2 = r_real - r_close_ven
        rows.append({"id": t["id"], "asset": asset, "dir": t["direction"],
                     "m1": m1, "m2": m2, "r_real": r_real, "r_close_ven": r_close_ven,
                     "n_wk": len(gaps)})

    print("=== Backtest weekend-hold (Parte B, trade reali gap-prone) ===")
    print(f"analizzati: {len(rows)} | scartati: {len(skipped)}")
    for sid, a, why in skipped:
        print(f"  scartato #{sid} {a}: {why}")

    print(f"\n{'id':>4} {'asset':<11} {'dir':<5} {'n_wk':>4} "
          f"{'M1_gap':>7} {'R_closeVen':>10} {'R_real':>7} {'M2_tieni':>8}")
    print("-" * 64)
    for r in rows:
        print(f"{r['id']:>4} {r['asset']:<11} {r['dir']:<5} {r['n_wk']:>4} "
              f"{r['m1']:>+7.3f} {r['r_close_ven']:>+10.3f} {r['r_real']:>+7.3f} {r['m2']:>+8.3f}")

    def agg(xs, key):
        v = [x[key] for x in xs]
        return (statistics.mean(v) if v else float("nan"),
                statistics.median(v) if v else float("nan"),
                min(v) if v else float("nan"),
                _pctl([abs(x) for x in v], 0.9) if v else float("nan"))

    def block(label, xs):
        if not xs:
            print(f"{label:<34} (vuoto)")
            return
        m1m, m1med, m1min, m1abs90 = agg(xs, "m1")
        m2m, m2med, m2min, _ = agg(xs, "m2")
        print(f"{label:<34} n={len(xs):>2} | "
              f"M1 media {m1m:+.3f} min {m1min:+.3f} |M1|p90 {m1abs90:.3f} | "
              f"E_tieni(M2) {m2m:+.3f} min {m2min:+.3f}")

    print("\n=== Aggregati ===")
    block("Tutti gap-prone", rows)
    srt = sorted(rows, key=lambda r: r["m2"])
    block("senza il singolo peggiore (M2)", srt[1:])
    block("senza #89 (outlier sprint5)", [r for r in rows if r["id"] != OUTLIER])
    print("\n--- per asset ---")
    for a in sorted({r["asset"] for r in rows}):
        block(a, [r for r in rows if r["asset"] == a])

    # Verdetto pre-registrato
    all_m2 = [r["m2"] for r in rows]
    all_m1 = [r["m1"] for r in rows]
    e_tieni = statistics.mean(all_m2) if all_m2 else float("nan")
    e_tieni_rob = statistics.mean([r["m2"] for r in srt[1:]]) if len(srt) > 1 else float("nan")
    m1_mean = statistics.mean(all_m1) if all_m1 else float("nan")
    tail_p90 = _pctl([abs(x) for x in all_m1], 0.9) if all_m1 else float("nan")

    print("\n=== VERDETTO (soglie pre-registrate) ===")
    print(f"E_tieni={e_tieni:+.3f} (robusto {e_tieni_rob:+.3f}) | "
          f"M1 medio={m1_mean:+.3f} | |M1|p90 (Parte B)={tail_p90:.3f}")
    regola_exp = (e_tieni <= -0.10 and e_tieni_rob <= -0.05 and m1_mean <= -0.05)
    solo_coda = (abs(e_tieni) < 0.10 and tail_p90 >= 0.5)
    if regola_exp:
        v = "REGOLA SU EXPECTANCY -> Fase 2 (chiusura venerdi deterministica)"
    elif solo_coda:
        v = "SOLO RISCHIO-CODA -> scelta esplicita utente (no edge)"
    else:
        v = "NON GIUSTIFICATO -> archiviare (Parte B); confrontare con Tail Parte A"
    print(f">>> {v} <<<")
    print("NB: Tail_p90 del gate viene dalla Parte A strutturale "
          "(jobs/weekend_gap_analysis.py), qui |M1|p90 e' solo sui trade reali.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
