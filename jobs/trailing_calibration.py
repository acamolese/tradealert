"""Simulazione calibrazione trailing (Sprint 4, gate pre-registrato).

Ricostruisce la traiettoria del profitto dei trade chiusi dalle candele 5m
(fallback 15m) Capital sulla vita reale opened->closed, poi simula l'uscita
sotto diverse regole di trailing. Confronta 4 varianti a trail-gap fisso con
l'opzione D attuale, con e senza i due Brent short #42/#49 (test anti-artefatto).

Assunzioni dichiarate (pre-registrate):
- orizzonte = vita reale del trade; le chiusure MANUALI reali sono ignorate,
  la simulazione lascia decidere SL-trailato / TP / close ultima candela;
- short si chiude all'ask, long al bid (come jobs/peak_analysis.py);
- se in una stessa candela vengono colpiti sia SL sia TP, si assume SL (conservativo).

Sola lettura. Serve sessione Capital (credenziali nel .env) + anon key DB.
Uso: PYTHONPATH=$PWD .venv/bin/python jobs/trailing_calibration.py
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
GAPS = [0.25, 0.50, 0.75, 1.00]

# --- replica opzione D (da src/position_monitor.py) ---
_STEP = 0.25
_TP_LOCKS = ((0.90, 0.65), (0.80, 0.45))


def _granular(profit_r: float) -> float:
    if profit_r < 1.0:
        return -0.5 + math.floor((profit_r - 0.5) / _STEP) * _STEP
    return math.floor((profit_r - 1.0) / _STEP) * _STEP


def _tp_lock(frac_tp, rr):
    if frac_tp is None or rr is None:
        return None
    for frac_min, lock_frac in _TP_LOCKS:
        if frac_tp >= frac_min:
            return lock_frac * rr
    return None


def offset_D(peak_r, frac_tp, rr):
    if peak_r < 0.5:
        return -1.0  # nessun trailing sotto 0.5R: SL iniziale a -1R
    off = _granular(peak_r)
    tl = _tp_lock(frac_tp, rr)
    return max(off, tl) if tl is not None else off


def offset_gap(G):
    def f(peak_r, frac_tp, rr):
        if peak_r < 0.5:
            return -1.0
        return max(-1.0, peak_r - G)
    return f


def _naive(ts):
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _fetch(cl, epic, frm, to):
    for res in ("MINUTE_5", "MINUTE_15", "MINUTE_30"):
        r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                            params={"resolution": res, "from": frm.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "to": to.strftime("%Y-%m-%dT%H:%M:%S"), "max": 1000}, timeout=30)
        if r.status_code == 200:
            p = r.json().get("prices", [])
            if p:
                return p, res
    return [], None


def _simulate(candles, direction, entry, r_dist, reward, rr, offset_fn):
    """Ritorna profit_r all'uscita simulata."""
    peak_r = 0.0
    peak_fav_move = 0.0
    for c in candles:
        # offset calcolato sul picco PRIMA di questa candela
        frac_tp = (peak_fav_move / reward) if reward else None
        off = offset_fn(peak_r, frac_tp, rr)
        if direction == "short":
            sl_price = entry - off * r_dist          # off=-0.5 -> sopra entry
            tp_price = entry - reward
            adv = c["highPrice"]["ask"]; fav = c["lowPrice"]["ask"]
            if adv >= sl_price:
                return off                            # SL trailato colpito
            if fav <= tp_price:
                return rr                             # TP colpito
            fav_move = entry - fav
        else:
            sl_price = entry + off * r_dist
            tp_price = entry + reward
            adv = c["lowPrice"]["bid"]; fav = c["highPrice"]["bid"]
            if adv <= sl_price:
                return off
            if fav >= tp_price:
                return rr
            fav_move = fav - entry
        if fav_move > peak_fav_move:
            peak_fav_move = fav_move
            peak_r = fav_move / r_dist
    # nessun trigger: esci al close ultima candela
    last = candles[-1]
    if direction == "short":
        return (entry - last["closePrice"]["ask"]) / r_dist
    return (last["closePrice"]["bid"] - entry) / r_dist


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    trades = (db._client.table("trades").select("*")
              .gte("id", ID_MIN).lte("id", ID_MAX).eq("status", "closed")
              .order("id").execute().data)

    strategies = {"D": offset_D}
    for g in GAPS:
        strategies[f"gap{g}"] = offset_gap(g)

    rows = []
    skipped = []
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        sl_pct = sig.get("stop_loss") if sig else None
        tp_pct = sig.get("take_profit") if sig else None
        epic = EPIC.get(t["asset"])
        if not (sl_pct and tp_pct and epic and t.get("closed_at")):
            skipped.append((t["id"], "manca sl/tp/epic/closed"))
            continue
        entry = float(t["entry_price"]); size = float(t["size"])
        r_dist = entry * float(sl_pct) / 100.0
        reward = entry * float(tp_pct) / 100.0
        rr = float(tp_pct) / float(sl_pct)
        frm = _naive(t["opened_at"]); to = _naive(t["closed_at"])
        candles, res = _fetch(cl, epic, frm, to)
        time.sleep(0.5)
        sel = [c for c in candles if frm <= datetime.fromisoformat(c["snapshotTimeUTC"]) <= to]
        if len(sel) < 3:
            skipped.append((t["id"], f"candele insufficienti ({len(sel)})"))
            continue
        # peak_R e MAE reali (traiettoria)
        if t["direction"] == "short":
            peakR = max((entry - c["lowPrice"]["ask"]) / r_dist for c in sel)
            maeR = min((entry - c["highPrice"]["ask"]) / r_dist for c in sel)
        else:
            peakR = max((c["highPrice"]["bid"] - entry) / r_dist for c in sel)
            maeR = min((c["lowPrice"]["bid"] - entry) / r_dist for c in sel)
        rec = {"id": t["id"], "asset": t["asset"], "dir": t["direction"],
               "size": size, "r_dist": r_dist, "rr": round(rr, 2),
               "peakR": round(peakR, 2), "maeR": round(maeR, 2),
               "exit_reason": (t.get("exit_reason") or "")[:20], "res": res,
               "pnl_real": float(t["pnl"]) if t.get("pnl") is not None else None}
        for name, fn in strategies.items():
            pr = _simulate(sel, t["direction"], entry, r_dist, reward, rr, fn)
            rec[name] = round(pr, 3)
            rec[name + "_eur"] = round(pr * r_dist * size, 2)
        rows.append(rec)

    print(f"=== Task 1: copertura ===")
    print(f"trade chiusi 38-64: {len(trades)} | simulati: {len(rows)} | scartati: {len(skipped)}")
    for sid, why in skipped:
        print(f"  scartato #{sid}: {why}")

    # tabella traiettoria + exit per strategia (in R)
    print(f"\n=== Traiettoria + exit_R per strategia ===")
    hdr = f"{'id':>3} {'asset':<10} {'dir':<5} {'peakR':>6} {'maeR':>6} {'rr':>4} | {'D':>6} {'g.25':>6} {'g.5':>6} {'g.75':>6} {'g1':>6}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['id']:>3} {r['asset']:<10} {r['dir']:<5} {r['peakR']:>6.2f} {r['maeR']:>6.2f} {r['rr']:>4.1f} | "
              f"{r['D']:>6.2f} {r['gap0.25']:>6.2f} {r['gap0.5']:>6.2f} {r['gap0.75']:>6.2f} {r['gap1.0']:>6.2f}")

    def totals(subset, label):
        print(f"\n=== TOTALI {label} (n={len(subset)}) ===")
        print(f"{'strategia':<10} {'R tot':>8} {'EUR tot':>9}")
        base_r = sum(r["D"] for r in subset)
        for name in ["D", "gap0.25", "gap0.5", "gap0.75", "gap1.0"]:
            rtot = sum(r[name] for r in subset)
            etot = sum(r[name + "_eur"] for r in subset)
            delta = rtot - base_r
            flag = "  <-- batte D" if (name != "D" and rtot > base_r) else ""
            print(f"{name:<10} {rtot:>8.2f} {etot:>9.2f}  (dR vs D {delta:+.2f}){flag}")

    totals(rows, "SAMPLE COMPLETO")
    totals([r for r in rows if r["id"] not in BRENT_KEY], "SENZA #42/#49")

    # Task 5: gap migliore per asset
    print(f"\n=== Task 5: R totale per asset e strategia ===")
    assets = sorted({r["asset"] for r in rows})
    print(f"{'asset':<11} {'n':>2} {'D':>7} {'g.25':>7} {'g.5':>7} {'g.75':>7} {'g1':>7}  best")
    for a in assets:
        sub = [r for r in rows if r["asset"] == a]
        vals = {name: sum(r[name] for r in sub) for name in ["D", "gap0.25", "gap0.5", "gap0.75", "gap1.0"]}
        best = max(vals, key=vals.get)
        print(f"{a:<11} {len(sub):>2} {vals['D']:>7.2f} {vals['gap0.25']:>7.2f} {vals['gap0.5']:>7.2f} {vals['gap0.75']:>7.2f} {vals['gap1.0']:>7.2f}  {best}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
