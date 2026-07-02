"""Validazione del simulatore a candele ORARIE contro la realta' (Sprint 7 M3).

Il backtest storico potra' usare solo candele HOUR (disponibili dal 2020).
Prima di fidarsi, il simulatore deve dimostrare di riprodurre gli esiti noti:

  Set 1 (ground truth reale): trade chiusi da stop_hit/tp_hit -> replay
        bracket+trailing su candele HOUR vs exit_R reale.
  Set 2 (ground truth 5m): trade chiusi dal monitor -> replay HOUR vs il
        controfattuale 5m di A1 (docs/sprint6-monitor-replay.csv). Misura il
        solo errore di risoluzione.

Gate pre-registrato (docs/sprint7-rethink.md M3): scarto medio aggregato
<= 0.10R su entrambi i set. Se fallisce: STOP, il simulatore va corretto
prima di qualsiasi backtest.

Sola lettura. Uso: PYTHONPATH=$PWD .venv/bin/python jobs/validate_sim.py
"""

from __future__ import annotations

import csv
import gzip
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.db import Database

from jobs.monitor_close_replay import (
    make_offset_fn, V1_LIVE, V2_LIVE, HORIZON_DAYS, EPIC_FALLBACK,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "candles"
GATE_TOLERANCE_R = 0.10


def load_candles(epic: str) -> list[dict]:
    path = DATA_DIR / f"{epic}_HOUR.csv.gz"
    if not path.exists():
        return []
    out = []
    with gzip.open(path, "rt") as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "ts": row["ts"],
                    "high_bid": float(row["high_bid"]), "high_ask": float(row["high_ask"]),
                    "low_bid": float(row["low_bid"]), "low_ask": float(row["low_ask"]),
                    "close_bid": float(row["close_bid"]), "close_ask": float(row["close_ask"]),
                })
            except (ValueError, TypeError):
                continue
    return out


def simulate_hour(candles, direction, entry, r_dist, reward, rr, offset_fn):
    """Stessa logica di monitor_close_replay._simulate, su righe CSV orarie."""
    peak_r = 0.0
    peak_fav = 0.0
    for c in candles:
        frac_tp = (peak_fav / reward) if reward else None
        off = offset_fn(peak_r, frac_tp, rr)
        if direction == "short":
            sl_price = entry - off * r_dist
            tp_price = entry - reward
            adv = c["high_ask"]; fav = c["low_ask"]
            if adv >= sl_price:
                return off, "sl"
            if fav <= tp_price:
                return rr, "tp"
            fav_move = entry - fav
        else:
            sl_price = entry + off * r_dist
            tp_price = entry + reward
            adv = c["low_bid"]; fav = c["high_bid"]
            if adv <= sl_price:
                return off, "sl"
            if fav >= tp_price:
                return rr, "tp"
            fav_move = fav - entry
        if fav_move > peak_fav:
            peak_fav = fav_move
            peak_r = fav_move / r_dist
    if not candles:
        return None, "no_data"
    last = candles[-1]
    if direction == "short":
        return (entry - last["close_ask"]) / r_dist, "horizon"
    return (last["close_bid"] - entry) / r_dist, "horizon"


def _naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def main() -> int:
    cfg = load_config()
    db = Database(cfg)

    cf5m = {}
    cf_path = Path("docs/sprint6-monitor-replay.csv")
    if cf_path.exists():
        with open(cf_path) as f:
            for row in csv.DictReader(f):
                cf5m[int(row["id"])] = float(row["cf_r"])

    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").not_.is_("pnl", "null")
              .order("id").execute().data)

    candle_cache: dict[str, list[dict]] = {}
    set1, set2, skipped = [], [], 0
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        sl_pct = sig.get("stop_loss") if sig else None
        tp_pct = sig.get("take_profit") if sig else None
        epic = (sig.get("epic") if sig else None) or EPIC_FALLBACK.get(t["asset"])
        reason = (t.get("exit_reason") or "")
        is_bracket = reason.startswith("reconcile:stop_hit") or reason.startswith("reconcile:tp_hit")
        is_monitor = t["id"] in cf5m
        if not (sl_pct and tp_pct and epic) or not (is_bracket or is_monitor):
            skipped += 1
            continue
        if epic not in candle_cache:
            candle_cache[epic] = load_candles(epic)
        if not candle_cache[epic]:
            skipped += 1
            continue
        entry = float(t["entry_price"])
        r_dist = entry * float(sl_pct) / 100.0
        reward = entry * float(tp_pct) / 100.0
        rr = float(tp_pct) / float(sl_pct)
        opened = _naive(t["opened_at"])
        closed = _naive(t["closed_at"])
        # stesso orizzonte di A1: bracket-only fino a chiusura reale (set 1)
        # o closed+10gg (set 2, come il controfattuale 5m)
        horizon = closed if is_bracket else closed + timedelta(days=HORIZON_DAYS)
        sel = [c for c in candle_cache[epic]
               if opened <= datetime.fromisoformat(c["ts"]) <= horizon]
        if len(sel) < 2:
            skipped += 1
            continue
        v1 = opened >= V1_LIVE
        v2 = opened >= V2_LIVE
        sim_r, sim_exit = simulate_hour(sel, t["direction"], entry, r_dist,
                                        reward, rr, make_offset_fn(v1, v2))
        if sim_r is None:
            skipped += 1
            continue
        if is_bracket:
            cp = float(t["close_price"]) if t.get("close_price") is not None else None
            if cp is None:
                skipped += 1
                continue
            real_r = ((entry - cp) if t["direction"] == "short" else (cp - entry)) / r_dist
            set1.append({"id": t["id"], "asset": t["asset"], "sim": sim_r,
                         "ref": real_r, "err": sim_r - real_r, "exit": sim_exit})
        else:
            set2.append({"id": t["id"], "asset": t["asset"], "sim": sim_r,
                         "ref": cf5m[t["id"]], "err": sim_r - cf5m[t["id"]],
                         "exit": sim_exit})

    def report(name, rows, ref_label):
        errs = [r["err"] for r in rows]
        mean_err = statistics.mean(errs)
        mae = statistics.mean(abs(e) for e in errs)
        print(f"\n=== {name} (n={len(rows)}, riferimento: {ref_label}) ===")
        print(f"errore medio (bias): {mean_err:+.3f}R | errore assoluto medio: {mae:.3f}R")
        print(f"somma R sim {sum(r['sim'] for r in rows):+.2f} vs ref {sum(r['ref'] for r in rows):+.2f}")
        worst = sorted(rows, key=lambda r: -abs(r["err"]))[:5]
        for r in worst:
            print(f"  peggiori: #{r['id']} {r['asset']:<12} sim {r['sim']:+.2f} "
                  f"ref {r['ref']:+.2f} err {r['err']:+.2f} ({r['exit']})")
        return mean_err, mae

    print(f"trade valutati: set1={len(set1)} set2={len(set2)} | esclusi: {skipped}")
    m1, a1_ = report("SET 1: bracket reali (stop/tp hit)", set1, "exit_R reale")
    m2, a2_ = report("SET 2: monitor-closed", set2, "controfattuale 5m A1")

    ok = abs(m1) <= GATE_TOLERANCE_R and abs(m2) <= GATE_TOLERANCE_R
    print(f"\n>>> GATE M3 (|bias| <= {GATE_TOLERANCE_R}R su entrambi i set): "
          f"{'PASSA' if ok else 'FALLISCE'} <<<")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
