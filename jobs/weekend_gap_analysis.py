"""FASE 1 — Misura del rischio gap di weekend/sessione (pre-registrato in
docs/sprint5-weekend-gap.md). Sola lettura.

Parte A (struttura di mercato): distribuzione dei gap tra candele consecutive
separate da chiusura sessione/weekend, per HK50/J225 vs Brent/Gold/Bitcoin,
in % e in R-equivalente (gap% / stop%_mediano).
Parte B (trade-based): slippage oltre lo stop sui trade reconcile:stop_hit.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/weekend_gap_analysis.py
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime, timezone, timedelta

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient

ASSETS = {"Hang Seng": "HK50", "Nikkei": "J225", "Brent Oil": "OIL_BRENT",
          "Gold": "GOLD", "Bitcoin": "BTCUSD"}
ASIAN = {"Hang Seng", "Nikkei"}
CONTINUOUS = {"Brent Oil", "Bitcoin"}
# conv quote->EUR per-asset, empirici (mediana storica pnl/(escursione*size))
CONV = {"Hang Seng": 0.1226, "Brent Oil": 1.030, "Gold": 0.983, "Bitcoin": 1.042}
DAYS = 40
GAP_MIN_H = 1.5   # buco > 1.5h tra candele HOUR = boundary di sessione/weekend


def _mid(p, side):
    return (p[side]["bid"] + p[side]["ask"]) / 2


def _fetch_hour(cl, epic, frm, to):
    out = []
    cur = frm
    # pagina a blocchi per coprire ~40gg (max 1000 candle/chiamata)
    while cur < to:
        chunk_to = min(cur + timedelta(days=20), to)
        try:
            r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                                params={"resolution": "HOUR",
                                        "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "to": chunk_to.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "max": 1000}, timeout=30)
            if r.status_code == 200:
                out += r.json().get("prices", [])
        except Exception:
            pass
        cur = chunk_to
        time.sleep(0.3)
    # dedup per snapshotTimeUTC
    seen = {}
    for p in out:
        seen[p["snapshotTimeUTC"]] = p
    return sorted(seen.values(), key=lambda p: p["snapshotTimeUTC"])


def _spans_weekend(t1, t2):
    d = t1.date()
    while d <= t2.date():
        if d.weekday() >= 5:   # 5=Sab 6=Dom
            return True
        d += timedelta(days=1)
    return False


def _pctl(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    # stop% mediano per asset dai signal
    sigs = (db._client.table("signals").select("asset,stop_loss")
            .gte("created_at", "2026-05-01").execute().data)
    stop_by_asset = {}
    for s in sigs:
        if s.get("stop_loss"):
            stop_by_asset.setdefault(s["asset"], []).append(float(s["stop_loss"]))
    med_stop = {a: round(statistics.median(v), 2) for a, v in stop_by_asset.items()}
    # J225 proxy = HK50
    if "Nikkei" not in med_stop:
        med_stop["Nikkei"] = med_stop.get("Hang Seng", 1.45)
    print("stop% mediano per asset:", med_stop)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    frm = now - timedelta(days=DAYS)

    # ===== PARTE A: struttura di mercato =====
    print(f"\n=== PARTE A: distribuzione gap di sessione/weekend ({DAYS}gg, HOUR) ===")
    print(f"{'asset':<10} {'tipo':<8} {'n':>3} {'gap%_med':>8} {'gap%_p90':>8} {'gap%_max':>8} "
          f"{'R_med':>6} {'R_p90':>6} {'R_max':>6}")
    print("-" * 78)
    partA = {}
    for asset, epic in ASSETS.items():
        candles = _fetch_hour(cl, epic, frm, now)
        st = med_stop.get(asset, 1.45)
        gaps = {"weekend": [], "daily": []}
        for a, b in zip(candles, candles[1:]):
            t1 = datetime.fromisoformat(a["snapshotTimeUTC"])
            t2 = datetime.fromisoformat(b["snapshotTimeUTC"])
            dt_h = (t2 - t1).total_seconds() / 3600
            if dt_h <= GAP_MIN_H:
                continue
            c1 = _mid(a, "closePrice"); o2 = _mid(b, "openPrice")
            if c1 <= 0:
                continue
            gpct = abs(o2 - c1) / c1 * 100
            kind = "weekend" if (_spans_weekend(t1, t2) or dt_h >= 48) else "daily"
            gaps[kind].append(gpct)
        partA[asset] = {}
        for kind in ("daily", "weekend"):
            xs = gaps[kind]
            if not xs:
                print(f"{asset:<10} {kind:<8} {0:>3}  {'-':>7} {'-':>8} {'-':>8} {'-':>6} {'-':>6} {'-':>6}")
                continue
            med = statistics.median(xs); p90 = _pctl(xs, 0.9); mx = max(xs)
            partA[asset][kind] = {"n": len(xs), "med": med, "p90": p90, "max": mx,
                                  "R_med": med / st, "R_p90": p90 / st, "R_max": mx / st}
            print(f"{asset:<10} {kind:<8} {len(xs):>3} {med:>8.3f} {p90:>8.3f} {mx:>8.3f} "
                  f"{med/st:>6.2f} {p90/st:>6.2f} {mx/st:>6.2f}")

    # confronto sintetico asiatici vs continui (weekend p90 R)
    print(f"\n--- Sintesi weekend R_p90 ---")
    cont_wk = [partA[a].get("weekend", {}).get("R_p90", 0) for a in CONTINUOUS]
    cont_max = max([x for x in cont_wk if x], default=0)
    for a in ASIAN:
        wk = partA.get(a, {}).get("weekend", {})
        dl = partA.get(a, {}).get("daily", {})
        rp90 = wk.get("R_p90"); drp90 = dl.get("R_p90")
        ratio = (rp90 / cont_max) if (rp90 and cont_max) else None
        print(f"  {a}: weekend R_p90={rp90} | daily R_p90={drp90} | "
              f"vs max(continui)={cont_max:.2f} -> {('%.1fx' % ratio) if ratio else 'n/a'}")

    # ===== PARTE B: slippage trade reali =====
    print(f"\n=== PARTE B: slippage oltre lo stop (trade reconcile:stop_hit) ===")
    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").order("id").execute().data)
    sig_stop = {s["id"]: s for s in db._client.table("signals").select("id,stop_loss").execute().data}
    print(f"{'id':>3} {'asset':<10} {'dir':<5} {'pnl€':>7} {'R_real':>7} {'slip_R':>7} {'slip€':>7} {'held_wknd':>9} {'dur_h':>6}")
    print("-" * 74)
    by_asset = {}
    for t in trades:
        if (t.get("exit_reason") or "") != "reconcile:stop_hit":
            continue
        asset = t["asset"]
        if asset not in CONV:
            continue
        sg = sig_stop.get(t.get("signal_id"), {})
        if not sg.get("stop_loss") or t.get("pnl") is None:
            continue
        entry = float(t["entry_price"]); size = float(t["size"]); stp = float(sg["stop_loss"])
        r_dist = entry * stp / 100.0
        r_eur = r_dist * size * CONV[asset]
        pnl = float(t["pnl"])
        r_real = pnl / r_eur if r_eur else None
        slip_R = max(0.0, -r_real - 1) if r_real is not None else None
        slip_e = slip_R * r_eur if slip_R is not None else None
        op = datetime.fromisoformat(t["opened_at"]).replace(tzinfo=None) if t.get("opened_at") else None
        cl_ = datetime.fromisoformat(t["closed_at"]).replace(tzinfo=None) if t.get("closed_at") else None
        held = _spans_weekend(op, cl_) if (op and cl_) else False
        dur = (cl_ - op).total_seconds() / 3600 if (op and cl_) else None
        by_asset.setdefault(asset, []).append((slip_R, held))
        print(f"{t['id']:>3} {asset:<10} {t['direction']:<5} {pnl:>7.2f} "
              f"{(r_real if r_real is not None else 0):>7.2f} {(slip_R or 0):>7.2f} "
              f"{(slip_e or 0):>7.2f} {str(held):>9} {(dur or 0):>6.1f}")

    print(f"\n--- Slippage medio per asset (stop_hit) ---")
    for a, xs in sorted(by_asset.items()):
        slips = [s for s, _ in xs if s is not None]
        held_slips = [s for s, h in xs if h and s is not None]
        print(f"  {a:<10} n={len(slips)} slip_R medio {statistics.mean(slips):+.2f} "
              f"max {max(slips):+.2f} | held-weekend n={len(held_slips)} "
              f"slip_R medio {(statistics.mean(held_slips) if held_slips else 0):+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
