"""Vista per-setup dettagliata degli ultimi N signal: feature alla decisione,
R:R, esito (pnl, R realizzato) e traiettoria (peakR/MAE-R ricostruiti dalle
candele Capital). Sola lettura. Diagnostica, non pre-registrata.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/setup_detail.py [N]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient

EPIC = {"Gold": "GOLD", "Brent Oil": "OIL_BRENT", "US500": "US500",
        "Nasdaq 100": "US100", "US Tech 100": "US100", "Bitcoin": "BTCUSD",
        "EUR/USD": "EURUSD", "AUD/USD": "AUDUSD", "GBP/USD": "GBPUSD",
        "Copper": "COPPER", "Hang Seng": "HK50", "Nikkei": "J225"}
EPS = 1e-9


def _naive(ts):
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _fetch(cl, epic, frm, to):
    for res in ("MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR"):
        try:
            r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                                params={"resolution": res,
                                        "from": frm.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "to": to.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "max": 1000}, timeout=30)
            if r.status_code == 200 and r.json().get("prices"):
                return r.json()["prices"], res
        except Exception:
            pass
    return [], None


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    sigs = (db._client.table("signals").select("*").order("id", desc=True)
            .limit(n).execute().data)
    sigs = list(reversed(sigs))
    sids = [s["id"] for s in sigs]
    trs = {t["signal_id"]: t for t in
           db._client.table("trades").select("*").in_("signal_id", sids).execute().data}

    rows = []
    for s in sigs:
        f = s.get("features_at_decision") or {}
        t = trs.get(s["id"], {})
        rec = {
            "sig": s["id"], "tr": t.get("id"), "asset": s["asset"],
            "dir": s["direction"], "score": s.get("score"),
            "stop": s.get("stop_loss"), "tgt": s.get("take_profit"),
            "rr": (float(s["take_profit"]) / float(s["stop_loss"]))
            if s.get("stop_loss") else None,
            "rsi": f.get("rsi_14"), "slope": f.get("trend_slope_pct"),
            "slope_s": f.get("trend_slope_short_pct"),
            "from_high20": f.get("pct_from_high_20"),
            "atr_pct": f.get("atr_pct_of_price"), "bb": f.get("bb_width_pct"),
            "spread": f.get("spread_pct"), "day_chg": f.get("daily_pct_change"),
            "pnl": float(t["pnl"]) if t.get("pnl") is not None else None,
            "exit": (t.get("exit_reason") or "")[:22], "status": t.get("status"),
            "thesis": (s.get("thesis") or "").strip(),
            "peakR": None, "maeR": None, "Rreal": None, "conv": None,
        }
        # traiettoria
        if t.get("opened_at") and t.get("closed_at") and s["asset"] in EPIC:
            entry = float(t["entry_price"]); size = float(t["size"])
            d = t["direction"]; stop = float(s["stop_loss"])
            r_dist = entry * stop / 100.0
            frm = _naive(t["opened_at"]); to = _naive(t["closed_at"])
            c, res = _fetch(cl, EPIC[s["asset"]], frm, to); time.sleep(0.35)
            sel = [x for x in c if frm <= datetime.fromisoformat(x["snapshotTimeUTC"]) <= to]
            if len(sel) >= 2:
                if d == "short":
                    peak = max((entry - x["lowPrice"]["ask"]) / r_dist for x in sel)
                    mae = min((entry - x["highPrice"]["ask"]) / r_dist for x in sel)
                    close_q = (entry - sel[-1]["closePrice"]["ask"]) * size
                else:
                    peak = max((x["highPrice"]["bid"] - entry) / r_dist for x in sel)
                    mae = min((x["lowPrice"]["bid"] - entry) / r_dist for x in sel)
                    close_q = (sel[-1]["closePrice"]["bid"] - entry) * size
                rec["peakR"] = round(peak, 2); rec["maeR"] = round(mae, 2)
                if rec["pnl"] is not None and abs(close_q) > EPS:
                    conv = rec["pnl"] / close_q
                    risk_eur = r_dist * size * conv
                    if abs(risk_eur) > EPS:
                        rec["Rreal"] = round(rec["pnl"] / risk_eur, 2)
                    rec["conv"] = round(conv, 4)
        rows.append(rec)

    # Tabella 1: setup + feature
    print("=== SETUP: feature alla decisione ===")
    h = (f"{'sig':>3} {'as':<6} {'dir':<5} {'sc':>4} {'R:R':>4} "
         f"{'rsi':>5} {'slope':>6} {'slpS':>6} {'fromHi':>6} {'atr%':>5} {'bb%':>5} {'spr%':>5} {'dChg':>5}")
    print(h); print("-" * len(h))
    for r in rows:
        ab = r["asset"][:6]
        print(f"{r['sig']:>3} {ab:<6} {r['dir']:<5} {r['score'] or 0:>4} "
              f"{(r['rr'] or 0):>4.1f} {r['rsi'] or 0:>5.1f} {r['slope'] or 0:>6.2f} "
              f"{r['slope_s'] or 0:>6.2f} {r['from_high20'] or 0:>6.2f} "
              f"{r['atr_pct'] or 0:>5.2f} {r['bb'] or 0:>5.2f} {r['spread'] or 0:>5.3f} "
              f"{r['day_chg'] or 0:>5.2f}")

    # Tabella 2: esito + traiettoria
    print("\n=== ESITO + traiettoria ===")
    h2 = (f"{'sig':>3} {'as':<6} {'dir':<5} {'pnl€':>7} {'Rreal':>6} "
          f"{'peakR':>6} {'maeR':>6} {'exit':<22}")
    print(h2); print("-" * len(h2))
    for r in rows:
        ab = r["asset"][:6]
        pnl = f"{r['pnl']:+.2f}" if r["pnl"] is not None else "NA"
        Rr = f"{r['Rreal']:+.2f}" if r["Rreal"] is not None else "  NA"
        pk = f"{r['peakR']:+.2f}" if r["peakR"] is not None else "  NA"
        ma = f"{r['maeR']:+.2f}" if r["maeR"] is not None else "  NA"
        print(f"{r['sig']:>3} {ab:<6} {r['dir']:<5} {pnl:>7} {Rr:>6} {pk:>6} {ma:>6} {r['exit']:<22}")

    # Tesi integrali
    print("\n=== TESI ===")
    for r in rows:
        print(f"#{r['sig']} {r['asset']} {r['dir']} (sc {r['score']}): {r['thesis']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
