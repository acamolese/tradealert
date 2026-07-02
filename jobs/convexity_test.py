"""Test di convessità sulle uscite: no-TP + time-stop (Sprint 7).

Protocollo pre-registrato in docs/sprint7-convexity-test.md. Usa le entrate
REALI dei trade chiusi e le candele HOUR locali (data/candles/), simulando
tre varianti di uscita sullo stesso stream: V0 bracket con TP reale,
V1 trailing-only senza TP, V2 no-TP + time-stop 72h sotto +0.5R.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/convexity_test.py
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import Database

from jobs.monitor_close_replay import make_offset_fn, EPIC_FALLBACK
from jobs.validate_sim import load_candles

HORIZON_DAYS = 15
TIME_STOP_H = 72
TIME_STOP_PEAK_R = 0.5
OFFSET_FN = make_offset_fn(True, True)  # trailing live D+V1+V2


def _naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def simulate(candles, direction, entry, r_dist, tp_reward, rr, opened,
             use_tp: bool, time_stop: bool):
    peak_r, peak_fav = 0.0, 0.0
    for c in candles:
        cts = datetime.fromisoformat(c["ts"])
        # time-stop: valutato a inizio barra, chiude al close della barra
        if time_stop and (cts - opened) >= timedelta(hours=TIME_STOP_H) \
                and peak_r < TIME_STOP_PEAK_R:
            px = c["close_ask"] if direction == "short" else c["close_bid"]
            r = (entry - px) / r_dist if direction == "short" else (px - entry) / r_dist
            return r, "timestop"
        frac_tp = (peak_fav / tp_reward) if (use_tp and tp_reward) else None
        off = OFFSET_FN(peak_r, frac_tp, rr if use_tp else None)
        if direction == "short":
            if c["high_ask"] >= entry - off * r_dist:
                return off, "sl"
            if use_tp and tp_reward and c["low_ask"] <= entry - tp_reward:
                return rr, "tp"
            fav = entry - c["low_ask"]
        else:
            if c["low_bid"] <= entry + off * r_dist:
                return off, "sl"
            if use_tp and tp_reward and c["high_bid"] >= entry + tp_reward:
                return rr, "tp"
            fav = c["high_bid"] - entry
        if fav > peak_fav:
            peak_fav = fav
            peak_r = fav / r_dist
    if not candles:
        return None, "no_data"
    last = candles[-1]
    px = last["close_ask"] if direction == "short" else last["close_bid"]
    r = (entry - px) / r_dist if direction == "short" else (px - entry) / r_dist
    return r, "horizon"


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").not_.is_("pnl", "null")
              .order("id").execute().data)

    cache: dict[str, list[dict]] = {}
    rows, skipped = [], 0
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        sl_pct = sig.get("stop_loss") if sig else None
        tp_pct = sig.get("take_profit") if sig else None
        epic = (sig.get("epic") if sig else None) or EPIC_FALLBACK.get(t["asset"])
        if not (sl_pct and tp_pct and epic):
            skipped += 1
            continue
        if epic not in cache:
            cache[epic] = load_candles(epic)
        if not cache[epic]:
            skipped += 1
            continue
        entry = float(t["entry_price"])
        r_dist = entry * float(sl_pct) / 100.0
        tp_reward = entry * float(tp_pct) / 100.0
        rr = float(tp_pct) / float(sl_pct)
        opened = _naive(t["opened_at"])
        horizon = opened + timedelta(days=HORIZON_DAYS)
        sel = [c for c in cache[epic]
               if opened <= datetime.fromisoformat(c["ts"]) <= horizon]
        if len(sel) < 3:
            skipped += 1
            continue
        rec = {"id": t["id"], "asset": t["asset"], "dir": t["direction"]}
        for name, use_tp, tstop in (("V0", True, False), ("V1", False, False),
                                    ("V2", False, True)):
            r, why = simulate(sel, t["direction"], entry, r_dist, tp_reward,
                              rr, opened, use_tp, tstop)
            rec[name], rec[name + "_why"] = r, why
        if any(rec[v] is None for v in ("V0", "V1", "V2")):
            skipped += 1
            continue
        rows.append(rec)

    print(f"trade simulati: {len(rows)} | esclusi: {skipped}\n")

    def rep(name, label):
        rs = sorted((r[name] for r in rows), reverse=True)
        mean, med = statistics.mean(rs), statistics.median(rs)
        drop2 = statistics.mean(rs[2:])
        exits = {}
        for r in rows:
            exits[r[name + "_why"]] = exits.get(r[name + "_why"], 0) + 1
        print(f"{label:<32} exp {mean:+.3f}R  mediana {med:+.3f}  tot {sum(rs):+7.2f}R  "
              f"senza top2 {drop2:+.3f}  esiti {exits}")
        return mean, med, drop2

    m0, med0, _ = rep("V0", "V0 bracket con TP (baseline)")
    m1, med1, d1 = rep("V1", "V1 no-TP trailing-only")
    m2, med2, d2 = rep("V2", "V2 no-TP + time-stop 72h/0.5R")

    for name, m, med, d in (("V1", m1, med1, d1), ("V2", m2, med2, d2)):
        ok = (m - m0 >= 0.15) and (d - m0 >= 0.10) and (med0 - med >= -0.10 or med >= med0 - 0.10)
        delta_med = med - med0
        verdict = "SUPPORTATA" if (m - m0 >= 0.15 and d - m0 >= 0.10 and delta_med >= -0.10) else "no"
        print(f"\n{name} vs V0: delta exp {m - m0:+.3f}R | delta senza-top2 {d - m0:+.3f}R | "
              f"delta mediana {delta_med:+.3f}R -> {verdict}")

    # top runner: cosa fa il no-TP sui 7 TP-hit reali?
    print("\nTrade con V1 - V0 piu' grande (il runner liberato):")
    for r in sorted(rows, key=lambda x: -(x["V1"] - x["V0"]))[:6]:
        print(f"  #{r['id']:>3} {r['asset']:<12} {r['dir']:<5} "
              f"V0 {r['V0']:+.2f} -> V1 {r['V1']:+.2f} ({r['V1_why']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
