"""Sprint 4 — V1 'fascia bassa pulita' vs gap0.25 vs D, fascia alta intatta.

V1 anticipa il lock SOLO in 0.5-1.0R e si riaggancia a D al breakeven a 1.0R
(niente raccordo a +0.25 in 1.0-1.25R: l'errore della minimal fix). Confronto
base + rumore-aware (fill SL al lato peggiore della candela + spread), make-or-
break #42/#49, controllo anti-taglio-trend (peak>=1.25R).

Sola lettura. Vedi pre-registrazione in docs/sprint4-trailing-v1-clean.md.
Uso: PYTHONPATH=$PWD .venv/bin/python jobs/trailing_v1_clean.py
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
DEFAULT_SPREAD_PCT = 0.02


def _granular(p):
    if p < 1.0:
        return -0.5 + math.floor((p - 0.5) / _STEP) * _STEP
    return math.floor((p - 1.0) / _STEP) * _STEP


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


def offset_V1(peak, frac_tp, rr):
    if peak < 0.5:
        return -1.0
    if peak >= 1.0:
        return offset_D(peak, frac_tp, rr)        # D PURO da 1.0R in su
    if peak < 0.75:
        g = -0.25 + (peak - 0.5) / 0.25 * 0.15    # -0.25 -> -0.10
    else:
        g = -0.10 + (peak - 0.75) / 0.25 * 0.10   # -0.10 -> 0.0
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


def _simulate(candles, direction, entry, r_dist, reward, rr, offset_fn, noise=False, spread_r=0.0):
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
                return ((entry - adv) / r_dist - spread_r) if noise else off
            if fav <= tp_price:
                return rr - spread_r if noise else rr
            fav_move = entry - fav
        else:
            sl_price = entry + off * r_dist
            tp_price = entry + reward
            adv = c["lowPrice"]["bid"]; fav = c["highPrice"]["bid"]
            if adv <= sl_price:
                return ((adv - entry) / r_dist - spread_r) if noise else off
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
    cfg = load_config(); db = Database(cfg); cl = CapitalClient(cfg); cl.login()
    trades = (db._client.table("trades").select("*").gte("id", ID_MIN).lte("id", ID_MAX)
              .eq("status", "closed").order("id").execute().data)
    STRAT = {"D": offset_D, "V1": offset_V1, "gap025": offset_gap025}
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
        spread_r = (spread_pct / 100.0 * entry) / r_dist
        frm = _naive(t["opened_at"]); to = _naive(t["closed_at"])
        candles, res = _fetch(cl, epic, frm, to); time.sleep(0.45)
        sel = [c for c in candles if frm <= datetime.fromisoformat(c["snapshotTimeUTC"]) <= to]
        if len(sel) < 3:
            continue
        if t["direction"] == "short":
            peakR = max((entry - c["lowPrice"]["ask"]) / r_dist for c in sel)
        else:
            peakR = max((c["highPrice"]["bid"] - entry) / r_dist for c in sel)
        band = "sotto" if peakR < 0.5 else ("bassa" if peakR < 1.0 else "alta")
        rec = {"id": t["id"], "asset": t["asset"], "peakR": round(peakR, 2), "band": band,
               "trend": peakR >= 1.25,
               "low6": t["id"] in {43, 44, 54, 57, 62, 63}}
        for name, fn in STRAT.items():
            rec[name] = round(_simulate(sel, t["direction"], entry, r_dist, reward, rr, fn), 3)
            rec[name + "_n"] = round(_simulate(sel, t["direction"], entry, r_dist, reward, rr, fn,
                                               noise=True, spread_r=spread_r), 3)
        rows.append(rec)

    def tot(sub, k):
        return round(sum(r[k] for r in sub), 2)

    print(f"=== sample: {len(rows)} trade ===")

    print("\n=== TASK 3: delta vs D per fascia (R, base) ===")
    print(f"{'fascia':<7} {'n':>2} {'D':>7} {'V1':>7} {'gap025':>7} | {'dV1':>6} {'dGap':>6}")
    for band in ["sotto", "bassa", "alta"]:
        sub = [r for r in rows if r["band"] == band]
        if sub:
            print(f"{band:<7} {len(sub):>2} {tot(sub,'D'):>7.2f} {tot(sub,'V1'):>7.2f} {tot(sub,'gap025'):>7.2f} | "
                  f"{tot(sub,'V1')-tot(sub,'D'):>+6.2f} {tot(sub,'gap025')-tot(sub,'D'):>+6.2f}")

    print("\n=== TASK 4: make-or-break (4 celle: base/noise x con/senza Brent) ===")
    def cell(sub, label):
        print(f"  [{label}] n={len(sub)}")
        for s in ["D", "V1", "gap025"]:
            rb, rn = tot(sub, s), tot(sub, s + "_n")
            db_, dn = rb - tot(sub, "D"), rn - tot(sub, "D_n")
            print(f"    {s:<7} base={rb:>6.2f} ({db_:+.2f})   noise={rn:>6.2f} ({dn:+.2f})")
    cell(rows, "CON #42/#49")
    cell([r for r in rows if r["id"] not in BRENT_KEY], "SENZA #42/#49")

    print("\n=== Controllo ANTI-TAGLIO-TREND (peak>=1.25R) ===")
    tr = [r for r in rows if r["trend"]]
    print(f"  n={len(tr)}  ids={[r['id'] for r in tr]}")
    for s in ["D", "V1", "gap025"]:
        ab, an = sum(r[s] for r in tr)/len(tr), sum(r[s+'_n'] for r in tr)/len(tr)
        print(f"    {s:<7} exit_R medio base={ab:+.3f} ({ab-sum(r['D'] for r in tr)/len(tr):+.3f})  noise={an:+.3f}")
    # per-trade trend: V1 vs D (deve essere identico salvo ritracci profondi)
    diff = [r for r in tr if abs(r["V1"] - r["D"]) > 0.001]
    print(f"  trend con V1 != D (base): {[(r['id'], r['D'], r['V1']) for r in diff] or 'NESSUNO'}")

    print("\n=== Fascia bassa (6 trade #43,44,54,57,62,63): exit_R medio ===")
    low = [r for r in rows if r["low6"]]
    for s in ["D", "V1", "gap025"]:
        print(f"    {s:<7} base={sum(r[s] for r in low)/len(low):+.3f}  noise={sum(r[s+'_n'] for r in low)/len(low):+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
