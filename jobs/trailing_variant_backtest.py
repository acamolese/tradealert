"""Sprint 8 — Ri-validazione calibrazione trailing su storico lungo.
Pre-registrato in docs/sprint8-trailing-revalidation.md. Sola lettura.

Stessi ENTRY v1-momentum (~6300, 2020-2026) per tutte le varianti di trailing:
isola l'effetto della sola regola di uscita. Confronta D / V1 / V1+V2(live) /
gap0.25 / gap0.5, con breakdown fascia-bassa vs trend e IS/OOS.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/trailing_variant_backtest.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta

from jobs.backtest_run import load, OVERNIGHT_PCT, OVERNIGHT_DEFAULT
from jobs.monitor_close_replay import make_offset_fn
from jobs.scan_frequency_backtest import EPICS, _prev_day_close, TARGET_RR, MAX_HOLD_DAYS
from jobs.exit_frequency_backtest import build_entries

IS_END = datetime(2024, 1, 1)


def offset_gap(G):
    def f(peak_r, frac_tp, rr):
        if peak_r < 0.5:
            return -1.0
        return max(-1.0, peak_r - G)
    return f


VARIANTS = {
    "D": make_offset_fn(False, False),
    "V1": make_offset_fn(True, False),
    "V1+V2(live)": make_offset_fn(True, True),
    "gap0.10": offset_gap(0.10),
    "gap0.15": offset_gap(0.15),
    "gap0.25": offset_gap(0.25),
    "gap0.50": offset_gap(0.50),
}


def simulate(d, epic, j, direction, r_dist, rr, offset_fn):
    """Ritorna (r_net, peak_r, full_tp). Copia del motore live con offset_fn."""
    entry = d["oa"][j] if direction == "long" else d["ob"][j]
    reward = rr * r_dist
    t = d["ts"][j - 1]
    horizon = t + timedelta(days=MAX_HOLD_DAYS)
    on_pct = OVERNIGHT_PCT.get(epic, OVERNIGHT_DEFAULT)
    peak_fav, peak_r = 0.0, 0.0
    exit_r, exit_ts, full_tp = None, None, False
    for k in range(j, d["n"]):
        if d["ts"][k] > horizon:
            px = d["ca"][k - 1] if direction == "short" else d["cb"][k - 1]
            exit_r = ((entry - px) if direction == "short" else (px - entry)) / r_dist
            exit_ts = d["ts"][k - 1]
            break
        frac_tp = peak_fav / reward if reward else None
        off = offset_fn(peak_r, frac_tp, rr)
        if direction == "short":
            if d["ha"][k] >= entry - off * r_dist:
                exit_r, exit_ts = off, d["ts"][k]; break
            if d["la"][k] <= entry - reward:
                exit_r, exit_ts, full_tp = rr, d["ts"][k], True; break
            fav = entry - d["la"][k]
        else:
            if d["lb"][k] <= entry + off * r_dist:
                exit_r, exit_ts = off, d["ts"][k]; break
            if d["hb"][k] >= entry + reward:
                exit_r, exit_ts, full_tp = rr, d["ts"][k], True; break
            fav = d["hb"][k] - entry
        if fav > peak_fav:
            peak_fav = fav; peak_r = fav / r_dist
    if exit_r is None:
        return None, peak_r, False
    nights = (exit_ts.date() - t.date()).days
    fee_r = nights * (on_pct / 100.0) * entry / r_dist
    return exit_r - fee_r, peak_r, full_tp


def stats(rs):
    return statistics.mean(rs) if rs else float("nan")


def main() -> int:
    print("=== Ri-validazione trailing su storico lungo (2020-2026) ===")
    data = {e: load(e) for e in EPICS}
    ref = {e: _prev_day_close(data[e]) for e in EPICS}
    entries = build_entries(data, ref)
    print(f"entry (stessi per tutte le varianti): {len(entries)}\n")

    # simula ogni variante su ogni entry; classifica per peak_r sotto D
    rows = []   # per entry: {var: r_net}, peak_D, full_tp_D, ts, epic
    for e, j, direction, r_dist in entries:
        rec = {"epic": e, "ts": data[e]["ts"][j]}
        rD, peakD, tpD = simulate(data[e], e, j, direction, r_dist, TARGET_RR, VARIANTS["D"])
        if rD is None:
            continue
        rec["peak"] = peakD
        rec["full_tp"] = tpD
        rec["r"] = {"D": rD}
        for name, fn in VARIANTS.items():
            if name == "D":
                continue
            rn, _, _ = simulate(data[e], e, j, direction, r_dist, TARGET_RR, fn)
            rec["r"][name] = rn if rn is not None else 0.0
        rows.append(rec)

    def col(name, subset=None):
        s = subset if subset is not None else rows
        return [r["r"][name] for r in s]

    print(f"{'variante':<14} {'exp_R':>8} {'tot_R':>9} {'vs D':>8}")
    print("-" * 44)
    expD = stats(col("D"))
    for name in VARIANTS:
        c = col(name)
        print(f"{name:<14} {stats(c):>+8.4f} {sum(c):>+9.1f} {stats(c)-expD:>+8.4f}")

    # breakdown fascia bassa (peak 0.5-1.0, no full TP) e trend (peak>=1.25)
    low = [r for r in rows if 0.5 <= r["peak"] < 1.0 and not r["full_tp"]]
    trend = [r for r in rows if r["peak"] >= 1.25]
    mid = [r for r in rows if 1.0 <= r["peak"] < 1.25]
    print(f"\n--- Fascia bassa (peak 0.5-1.0R, no TP): n={len(low)} ---")
    for name in VARIANTS:
        print(f"  {name:<14} exp_R {stats(col(name, low)):+.4f}  (vs D {stats(col(name,low))-stats(col('D',low)):+.4f})")
    print(f"--- Trend (peak>=1.25R): n={len(trend)} ---")
    for name in VARIANTS:
        print(f"  {name:<14} exp_R {stats(col(name, trend)):+.4f}  (vs D {stats(col(name,trend))-stats(col('D',trend)):+.4f})")
    print(f"--- Media (peak 1.0-1.25R): n={len(mid)} ---")
    for name in ("D", "V1", "V1+V2(live)"):
        print(f"  {name:<14} exp_R {stats(col(name, mid)):+.4f}")

    # IS vs OOS
    is_r = [r for r in rows if r["ts"] < IS_END]
    oos = [r for r in rows if r["ts"] >= IS_END]
    print(f"\n--- IS 2020-2023 (n={len(is_r)}) vs OOS 2024-2026 (n={len(oos)}), exp_R ---")
    print(f"{'variante':<14} {'IS':>9} {'OOS':>9} {'IS vsD':>8} {'OOS vsD':>8}")
    for name in VARIANTS:
        eis, eoos = stats(col(name, is_r)), stats(col(name, oos))
        print(f"{name:<14} {eis:>+9.4f} {eoos:>+9.4f} "
              f"{eis-stats(col('D',is_r)):>+8.4f} {eoos-stats(col('D',oos)):>+8.4f}")

    # robustezza: senza i 2 migliori trade (per variante)
    print(f"\n--- Robustezza: exp_R senza i 2 migliori trade della variante ---")
    for name in ("V1", "gap0.25"):
        c = sorted(col(name), reverse=True)
        drop2 = stats(c[2:])
        cD = sorted(col("D"), reverse=True)
        print(f"  {name} senza top2 {drop2:+.4f} vs D senza top2 {stats(cD[2:]):+.4f} = {drop2-stats(cD[2:]):+.4f}")

    # verdetto pre-registrato
    e_v1, e_gap = stats(col("V1")), stats(col("gap0.25"))
    v1_low = stats(col("V1", low)) - stats(col("D", low))
    v1_trend = stats(col("V1", trend)) - stats(col("D", trend))
    v1_is = stats(col("V1", is_r)) - stats(col("D", is_r))
    v1_oos = stats(col("V1", oos)) - stats(col("D", oos))
    v1_drop2 = stats(sorted(col("V1"), reverse=True)[2:]) - stats(sorted(col("D"), reverse=True)[2:])
    print("\n=== VERDETTO (gate pre-registrato) ===")
    print(f"V1-D: tot {e_v1-expD:+.4f} | robust(senza top2) {v1_drop2:+.4f} | "
          f"IS {v1_is:+.4f} OOS {v1_oos:+.4f} | delta-trend {v1_trend:+.4f}")
    v1_ok = (e_v1 - expD >= 0.03 and v1_drop2 >= 0.01 and v1_is > 0 and v1_oos > 0 and v1_trend >= -0.02)
    gap_ok = (e_gap - e_v1 >= 0.05)
    if gap_ok:
        v = "gap0.25 SUPERIORE a V1 -> riaprire gap0.25"
    elif v1_ok:
        v = "V1 CONFERMATA (batte D robusto, non taglia i trend)"
    else:
        v = "NULLA BATTE D robusto -> rivalutare"
    print(f">>> {v} <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
