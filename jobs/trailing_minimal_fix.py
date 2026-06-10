"""Sprint 4 — minimal fix del trailing: anticipo del lock nella fascia 0.5-1.0R.

Confronta 3 strategie (D attuale, minimal fix, gap 0.25 aggressivo) sul sample
38-64, in modalita' base e RUMORE-AWARE (fill dello SL sul lato peggiore della
candela di hit + costo spread). Scompone il vantaggio per fascia di peak_R e
fa il make-or-break #42/#49.

Sola lettura (candele Capital + DB anon). Vedi pre-registrazione in
docs/sprint4-trailing-minimal-fix.md. Uso:
    PYTHONPATH=$PWD .venv/bin/python jobs/trailing_minimal_fix.py
"""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timezone

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient

ID_MIN, ID_MAX = 38, 64
BRENT_KEY = {42, 49}
EPIC = {"Gold": "GOLD", "Brent Oil": "OIL_BRENT", "US500": "US500",
        "Nasdaq 100": "US100", "Bitcoin": "BTCUSD"}
_STEP = 0.25
_TP_LOCKS = ((0.90, 0.65), (0.80, 0.45))
DEFAULT_SPREAD_PCT = 0.02  # fallback se manca spread_pct nelle features


def _granular(profit_r):
    if profit_r < 1.0:
        return -0.5 + math.floor((profit_r - 0.5) / _STEP) * _STEP
    return math.floor((profit_r - 1.0) / _STEP) * _STEP


def _tp_lock(frac_tp, rr):
    if frac_tp is None or rr is None:
        return None
    for fmin, lock in _TP_LOCKS:
        if frac_tp >= fmin:
            return lock * rr
    return None


def offset_D(peak, frac_tp, rr):
    if peak < 0.5:
        return -1.0
    off = _granular(peak)
    tl = _tp_lock(frac_tp, rr)
    return max(off, tl) if tl is not None else off


def offset_minimal(peak, frac_tp, rr):
    if peak < 0.5:
        return -1.0
    if peak <= 1.0:
        g = _granular(peak) + 0.25
    elif peak < 1.25:
        g = 0.25
    else:
        g = _granular(peak)
    tl = _tp_lock(frac_tp, rr)
    return max(g, tl) if tl is not None else g


def offset_gap025(peak, frac_tp, rr):
    if peak < 0.5:
        return -1.0
    return max(-1.0, peak - 0.25)


def _naive(ts):
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _fetch(cl, epic, frm, to):
    for res in ("MINUTE_5", "MINUTE_15", "MINUTE_30"):
        r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                            params={"resolution": res, "from": frm.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "to": to.strftime("%Y-%m-%dT%H:%M:%S"), "max": 1000}, timeout=30)
        if r.status_code == 200 and r.json().get("prices"):
            return r.json()["prices"], res
    return [], None


def _simulate(candles, direction, entry, r_dist, reward, rr, offset_fn,
              noise=False, spread_r=0.0):
    """profit_r all'uscita. noise=True: fill SL al lato peggiore della candela
    di hit + costo spread su ogni uscita."""
    peak_r = 0.0
    peak_fav = 0.0
    for c in candles:
        frac_tp = (peak_fav / reward) if reward else None
        off = offset_fn(peak_r, frac_tp, rr)
        if direction == "short":
            sl_price = entry - off * r_dist
            tp_price = entry - reward
            adv = c["highPrice"]["ask"]; fav = c["lowPrice"]["ask"]
            if adv >= sl_price:
                if noise:  # fill al lato peggiore (adverse extreme), oltre lo SL
                    pr = (entry - adv) / r_dist - spread_r
                    return pr
                return off - spread_r if noise else off
            if fav <= tp_price:
                return rr - spread_r if noise else rr
            fav_move = entry - fav
        else:
            sl_price = entry + off * r_dist
            tp_price = entry + reward
            adv = c["lowPrice"]["bid"]; fav = c["highPrice"]["bid"]
            if adv <= sl_price:
                if noise:
                    pr = (adv - entry) / r_dist - spread_r
                    return pr
                return off
            if fav >= tp_price:
                return rr - spread_r if noise else rr
            fav_move = fav - entry
        if fav_move > peak_fav:
            peak_fav = fav_move
            peak_r = fav_move / r_dist
    last = candles[-1]
    if direction == "short":
        base = (entry - last["closePrice"]["ask"]) / r_dist
    else:
        base = (last["closePrice"]["bid"] - entry) / r_dist
    return base - spread_r if noise else base


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    trades = (db._client.table("trades").select("*")
              .gte("id", ID_MIN).lte("id", ID_MAX).eq("status", "closed")
              .order("id").execute().data)

    STRAT = {"D": offset_D, "minimal": offset_minimal, "gap025": offset_gap025}
    rows = []
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        f = (sig or {}).get("features_at_decision") or {}
        sl_pct = sig.get("stop_loss") if sig else None
        tp_pct = sig.get("take_profit") if sig else None
        epic = EPIC.get(t["asset"])
        if not (sl_pct and tp_pct and epic and t.get("closed_at")):
            continue
        entry = float(t["entry_price"]); size = float(t["size"])
        r_dist = entry * float(sl_pct) / 100.0
        reward = entry * float(tp_pct) / 100.0
        rr = float(tp_pct) / float(sl_pct)
        spread_pct = f.get("spread_pct") or DEFAULT_SPREAD_PCT
        spread_r = (spread_pct / 100.0 * entry) / r_dist  # 1 spread in unita' R
        frm = _naive(t["opened_at"]); to = _naive(t["closed_at"])
        candles, res = _fetch(cl, epic, frm, to)
        time.sleep(0.45)
        sel = [c for c in candles if frm <= datetime.fromisoformat(c["snapshotTimeUTC"]) <= to]
        if len(sel) < 3:
            continue
        if t["direction"] == "short":
            peakR = max((entry - c["lowPrice"]["ask"]) / r_dist for c in sel)
        else:
            peakR = max((c["highPrice"]["bid"] - entry) / r_dist for c in sel)
        band = "sotto" if peakR < 0.5 else ("bassa" if peakR < 1.0 else "alta")
        rec = {"id": t["id"], "asset": t["asset"], "dir": t["direction"],
               "size": size, "r_dist": r_dist, "peakR": round(peakR, 2),
               "band": band, "full_tp": "tp_hit" in (t.get("exit_reason") or "").lower(),
               "spread_r": round(spread_r, 4)}
        for name, fn in STRAT.items():
            rec[name] = round(_simulate(sel, t["direction"], entry, r_dist, reward, rr, fn), 3)
            rec[name + "_n"] = round(_simulate(sel, t["direction"], entry, r_dist, reward, rr, fn,
                                               noise=True, spread_r=spread_r), 3)
        rows.append(rec)

    def tot(sub, key):
        return round(sum(r[key] for r in sub), 2)

    print(f"=== sample: {len(rows)} trade simulati ===")
    print(f"spread_r medio: {round(sum(r['spread_r'] for r in rows)/len(rows),4)} R")

    # Task 2 — scomposizione del vantaggio per fascia (modalita' base)
    print("\n=== TASK 2: delta vs D per fascia di peak_R (R, modalita' base) ===")
    print(f"{'fascia':<7} {'n':>2} {'D':>7} {'minimal':>8} {'gap025':>7} | {'dMin':>6} {'dGap':>6}")
    for band in ["sotto", "bassa", "alta"]:
        sub = [r for r in rows if r["band"] == band]
        if not sub:
            continue
        dmin = tot(sub, "minimal") - tot(sub, "D")
        dgap = tot(sub, "gap025") - tot(sub, "D")
        print(f"{band:<7} {len(sub):>2} {tot(sub,'D'):>7.2f} {tot(sub,'minimal'):>8.2f} {tot(sub,'gap025'):>7.2f} | {dmin:>+6.2f} {dgap:>+6.2f}")

    # Task 3 — confronto base vs rumore-aware
    def block(sub, label):
        print(f"\n=== {label} (n={len(sub)}) ===")
        print(f"{'strat':<9} {'R base':>8} {'R noise':>8} {'perso':>7} {'dVsD base':>10} {'dVsD noise':>11}")
        dbase = tot(sub, "D"); dnoise = tot(sub, "D_n")
        for s in ["D", "minimal", "gap025"]:
            rb = tot(sub, s); rn = tot(sub, s + "_n")
            print(f"{s:<9} {rb:>8.2f} {rn:>8.2f} {rb-rn:>7.2f} {rb-dbase:>+10.2f} {rn-dnoise:>+11.2f}")

    block(rows, "TASK 3: SAMPLE COMPLETO — base vs rumore-aware")
    # Task 4 — make-or-break
    block([r for r in rows if r["id"] not in BRENT_KEY], "TASK 4: SENZA #42/#49")

    # Task 5 baseline — trade fascia bassa (peak in [0.5,1.0), no TP) sotto D
    print("\n=== TASK 5 baseline: trade fascia bassa (peak 0.5-1.0R, no full TP) ===")
    low = [r for r in rows if r["band"] == "bassa" and not r["full_tp"]]
    print(f"  n={len(low)}  ids={[r['id'] for r in low]}")
    for s in ["D", "minimal", "gap025"]:
        avg_b = sum(r[s] for r in low) / len(low)
        avg_n = sum(r[s + '_n'] for r in low) / len(low)
        print(f"  {s:<8} exit_R medio base={avg_b:+.3f}  noise={avg_n:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
