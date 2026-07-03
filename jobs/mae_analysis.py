"""Analisi MAE: soglia di non-ritorno prima dello SL (Sprint 7).

Protocollo pre-registrato in docs/sprint7-mae-analysis.md.
Vista A: MAE sulla vita reale del trade vs esito reale (cio' che l'utente
vede sul telefono). Vista B: policy sim-vs-sim, pavimento dello stop a -X
contro il bracket attuale, sulle stesse entrate reali e candele HOUR locali.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/mae_analysis.py
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import Database

from jobs.monitor_close_replay import make_offset_fn, EPIC_FALLBACK
from jobs.validate_sim import load_candles

THRESHOLDS_DESC = [0.25, 0.40, 0.50, 0.60, 0.75]
THRESHOLDS_KILL = [0.40, 0.50, 0.60, 0.75]
HORIZON_DAYS = 15
BASE_FN = make_offset_fn(True, True)  # trailing live D+V1+V2


def _naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def path_r(candles, direction, entry, r_dist):
    """Serie (adverse_r_cum, fav_r) per barra: escursione peggiore e migliore."""
    out = []
    for c in candles:
        if direction == "short":
            adv = (entry - c["high_ask"]) / r_dist   # negativo se contro
            fav = (entry - c["low_ask"]) / r_dist
        else:
            adv = (c["low_bid"] - entry) / r_dist
            fav = (c["high_bid"] - entry) / r_dist
        out.append((adv, fav))
    return out


def simulate(candles, direction, entry, r_dist, tp_reward, rr, floor_x=None):
    """Bracket con TP + trailing live; opzionale pavimento stop a -floor_x.
    Ritorna (exit_r, mae_prima_delluscita)."""
    peak_r, peak_fav, mae = 0.0, 0.0, 0.0
    for c in candles:
        frac_tp = (peak_fav / tp_reward) if tp_reward else None
        off = BASE_FN(peak_r, frac_tp, rr)
        if floor_x is not None:
            off = max(off, -floor_x)
        if direction == "short":
            adv_px, fav_px = c["high_ask"], c["low_ask"]
            adv_r = (entry - adv_px) / r_dist
            if adv_px >= entry - off * r_dist:
                return off, min(mae, off)
            if fav_px <= entry - tp_reward:
                return rr, mae
            fav_move = entry - fav_px
        else:
            adv_px, fav_px = c["low_bid"], c["high_bid"]
            adv_r = (adv_px - entry) / r_dist
            if adv_px <= entry + off * r_dist:
                return off, min(mae, off)
            if fav_px >= entry + tp_reward:
                return rr, mae
            fav_move = fav_px - entry
        mae = min(mae, adv_r)
        if fav_move > peak_fav:
            peak_fav = fav_move
            peak_r = fav_move / r_dist
    if not candles:
        return None, None
    last = candles[-1]
    px = last["close_ask"] if direction == "short" else last["close_bid"]
    r = (entry - px) / r_dist if direction == "short" else (px - entry) / r_dist
    return r, mae


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
        if not (sl_pct and tp_pct and epic and t.get("close_price")):
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
        opened, closed = _naive(t["opened_at"]), _naive(t["closed_at"])
        life = [c for c in cache[epic]
                if opened <= datetime.fromisoformat(c["ts"]) <= closed]
        sim_w = [c for c in cache[epic]
                 if opened <= datetime.fromisoformat(c["ts"]) <= opened + timedelta(days=HORIZON_DAYS)]
        if len(life) < 2 or len(sim_w) < 3:
            skipped += 1
            continue
        cp = float(t["close_price"])
        real_r = ((entry - cp) if t["direction"] == "short" else (cp - entry)) / r_dist
        mae_real = min(a for a, _ in path_r(life, t["direction"], entry, r_dist))
        rec = {"id": t["id"], "asset": t["asset"], "real_r": real_r,
               "mae_real": mae_real}
        v0, mae0 = simulate(sim_w, t["direction"], entry, r_dist, tp_reward, rr)
        rec["V0"], rec["mae_V0"] = v0, mae0
        for x in THRESHOLDS_KILL:
            rec[f"K{x}"], _ = simulate(sim_w, t["direction"], entry, r_dist,
                                       tp_reward, rr, floor_x=x)
        if v0 is None or any(rec[f"K{x}"] is None for x in THRESHOLDS_KILL):
            skipped += 1
            continue
        rows.append(rec)

    print(f"trade analizzati: {len(rows)} | esclusi: {skipped}")

    print("\n=== VISTA A: vita reale (incluse chiusure monitor) ===")
    print("soglia | toccata da | chiusi >0 | chiusi >+0.5R | exit_R medio se toccata")
    for x in THRESHOLDS_DESC:
        hit = [r for r in rows if r["mae_real"] <= -x]
        if not hit:
            continue
        pos = sum(1 for r in hit if r["real_r"] > 0)
        big = sum(1 for r in hit if r["real_r"] > 0.5)
        m = statistics.mean(r["real_r"] for r in hit)
        print(f" -{x:.2f}R | {len(hit):>3}/{len(rows)} | {pos:>3} ({pos/len(hit):4.0%}) | "
              f"{big:>3} ({big/len(hit):4.0%}) | {m:+.3f}R")

    print("\n=== VISTA A-bis: simulazione bracket-only (senza monitor, orizzonte 15gg) ===")
    for x in THRESHOLDS_DESC:
        hit = [r for r in rows if r["mae_V0"] is not None and r["mae_V0"] <= -x]
        if not hit:
            continue
        pos = sum(1 for r in hit if r["V0"] > 0)
        m = statistics.mean(r["V0"] for r in hit)
        print(f" -{x:.2f}R | {len(hit):>3}/{len(rows)} | recupero >0: {pos:>3} ({pos/len(hit):4.0%}) | "
              f"exit medio {m:+.3f}R")

    print("\n=== VISTA B: policy 'pavimento stop a -X' vs bracket attuale ===")
    m0 = statistics.mean(r["V0"] for r in rows)
    print(f"V0 bracket attuale: exp {m0:+.3f}R  tot {sum(r['V0'] for r in rows):+.2f}R")
    for x in THRESHOLDS_KILL:
        vals = [r[f"K{x}"] for r in rows]
        mk = statistics.mean(vals)
        deltas = sorted((r[f"K{x}"] - r["V0"] for r in rows), reverse=True)
        drop2 = statistics.mean(deltas[2:])
        verdict = "SUPPORTATA" if (mk - m0 >= 0.15 and drop2 >= 0.10) else "no"
        print(f"KILL -{x:.2f}R: exp {mk:+.3f}R (delta {mk-m0:+.3f}) "
              f"tot {sum(vals):+.2f}R | delta senza top2 {drop2:+.3f} -> {verdict}")

    print("\nTrade che toccano -0.5R e chiudono comunque >+0.5R (i 'ritornati'):")
    for r in rows:
        if r["mae_real"] <= -0.5 and r["real_r"] > 0.5:
            print(f"  #{r['id']:>3} {r['asset']:<12} MAE {r['mae_real']:+.2f}R -> exit {r['real_r']:+.2f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
