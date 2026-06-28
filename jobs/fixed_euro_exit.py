"""Simulazione uscita a soglia fissa in euro (+Y / -X), pre-registrata in
docs/sprint5-fixed-euro-exit.md. Sola lettura.

Per ogni trade chiuso ricostruisce la traiettoria del P&L in euro dalle candele
Capital 5m (fallback 15m/30m) sulla vita reale opened->closed, poi simula
l'uscita al primo tocco di +Y euro (TP) o -X euro (SL). Confronta la somma EUR
del bracket con il pnl reale. Headline +1/-1; in piu' una griglia.

Conversione quote->EUR: conv = pnl_reale / (escursione_quote_chiusura * size),
con fallback alla mediana per-asset per i trade con pnl~0.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/fixed_euro_exit.py
"""
from __future__ import annotations

import statistics
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

HEADLINE = (1.0, 1.0)            # (stop_X, target_Y)
GRID_X = [1.0, 2.0, 3.0]
GRID_Y = [1.0, 2.0, 3.0]
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


def _excursions_eur(candles, direction, entry, size, conv):
    """Per ogni candela ritorna (fav_eur, adv_eur) cumulate sulla candela e il
    close_eur finale. Restituisce lista [(fav,adv)] + close_eur."""
    out = []
    for c in candles:
        if direction == "short":
            fav_q = entry - c["lowPrice"]["ask"]   # favorevole: prezzo scende
            adv_q = entry - c["highPrice"]["ask"]  # avverso: prezzo sale (negativo)
        else:
            fav_q = c["highPrice"]["bid"] - entry
            adv_q = c["lowPrice"]["bid"] - entry
        out.append((fav_q * size * conv, adv_q * size * conv))
    last = candles[-1]
    if direction == "short":
        close_eur = (entry - last["closePrice"]["ask"]) * size * conv
    else:
        close_eur = (last["closePrice"]["bid"] - entry) * size * conv
    return out, close_eur


def _bracket(excursions, close_eur, stop_x, target_y):
    """First-touch: -stop_x o +target_y. SL prima di TP nella stessa candela."""
    for fav, adv in excursions:
        hit_sl = adv <= -stop_x
        hit_tp = fav >= target_y
        if hit_sl:                 # conservativo
            return -stop_x
        if hit_tp:
            return target_y
    return close_eur


def _close_eur(candles, direction, entry, size):
    last = candles[-1]
    if direction == "short":
        return (entry - last["closePrice"]["ask"]) * size
    return (last["closePrice"]["bid"] - entry) * size


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").order("id").execute().data)
    trades = [t for t in trades
              if t.get("pnl") is not None and t.get("asset") in EPIC
              and t.get("opened_at") and t.get("closed_at")]

    # Passo 1: ricostruisci candele + escursione di chiusura in quote, raccogli conv
    recs = []
    skipped = []
    for t in trades:
        epic = EPIC[t["asset"]]
        entry = float(t["entry_price"]); size = float(t["size"])
        direction = t["direction"]; pnl = float(t["pnl"])
        frm = _naive(t["opened_at"]); to = _naive(t["closed_at"])
        candles, res = _fetch(cl, epic, frm, to)
        time.sleep(0.4)
        sel = [c for c in candles
               if frm <= datetime.fromisoformat(c["snapshotTimeUTC"]) <= to]
        if len(sel) < 3:
            skipped.append((t["id"], t["asset"], f"candele {len(sel)}"))
            continue
        close_q = _close_eur(sel, direction, entry, size)  # in quote*size
        conv_raw = (pnl / close_q) if abs(close_q) > EPS else None
        recs.append({"id": t["id"], "asset": t["asset"], "dir": direction,
                     "entry": entry, "size": size, "pnl": pnl,
                     "candles": sel, "close_q": close_q, "conv_raw": conv_raw,
                     "res": res, "exit": (t.get("exit_reason") or "")[:16]})

    # Passo 2: conv per-asset (mediana dei conv validi), fallback gruppo USD
    by_asset = {}
    for r in recs:
        if r["conv_raw"] is not None and r["conv_raw"] > 0:
            by_asset.setdefault(r["asset"], []).append(r["conv_raw"])
    asset_conv = {a: statistics.median(v) for a, v in by_asset.items()}
    all_valid = [c for v in by_asset.values() for c in v]
    usd_med = statistics.median(all_valid) if all_valid else 1.0
    for r in recs:
        if r["conv_raw"] is not None and r["conv_raw"] > 0:
            r["conv"] = r["conv_raw"]
        else:
            r["conv"] = asset_conv.get(r["asset"], usd_med)

    # Passo 3: simula bracket
    for r in recs:
        exc, close_eur = _excursions_eur(r["candles"], r["dir"], r["entry"],
                                         r["size"], r["conv"])
        r["exc"] = exc
        r["close_eur_sim"] = close_eur
        r[f"b_{HEADLINE[0]}_{HEADLINE[1]}"] = _bracket(exc, close_eur, *HEADLINE)

    print("=== Copertura ===")
    print(f"trade candidati: {len(trades)} | simulati: {len(recs)} | scartati: {len(skipped)}")
    for sid, a, why in skipped:
        print(f"  scartato #{sid} {a}: {why}")
    print(f"conv per-asset (mediana): " +
          ", ".join(f"{a}={c:.4f}" for a, c in sorted(asset_conv.items())))

    # Tabella per-trade headline
    sx, sy = HEADLINE
    key = f"b_{sx}_{sy}"
    print(f"\n=== Per-trade: reale vs bracket +{sy:g}/-{sx:g} ===")
    hdr = f"{'id':>3} {'asset':<10} {'dir':<5} {'pnl_real':>9} {'bracket':>8}  {'exit_real':<16}"
    print(hdr); print("-" * len(hdr))
    for r in recs:
        print(f"{r['id']:>3} {r['asset']:<10} {r['dir']:<5} {r['pnl']:>9.2f} "
              f"{r[key]:>8.2f}  {r['exit']:<16}")

    real_tot = sum(r["pnl"] for r in recs)
    br_tot = sum(r[key] for r in recs)
    real_win = sum(1 for r in recs if r["pnl"] > 0)
    br_win = sum(1 for r in recs if r[key] > 0)
    n = len(recs)
    print(f"\n=== HEADLINE +{sy:g}/-{sx:g} (n={n}) ===")
    print(f"  REALE   : tot {real_tot:+.2f}€ | media {real_tot/n:+.3f}€ | win {real_win}/{n} ({100*real_win/n:.0f}%)")
    print(f"  BRACKET : tot {br_tot:+.2f}€ | media {br_tot/n:+.3f}€ | win {br_win}/{n} ({100*br_win/n:.0f}%)")
    print(f"  delta totale: {br_tot-real_tot:+.2f}€")
    print(f"  criterio: > reale+5 approfondire | < reale NEGATIVO | in mezzo neutro")

    # Griglia
    print(f"\n=== Griglia totale EUR bracket (riga=stop -X, col=target +Y) ===")
    print(f"{'  -X\\+Y':>7}" + "".join(f"{y:>9g}" for y in GRID_Y))
    for x in GRID_X:
        cells = []
        for y in GRID_Y:
            tot = sum(_bracket(r["exc"], r["close_eur_sim"], x, y) for r in recs)
            cells.append(f"{tot:>9.2f}")
        print(f"{x:>7g}" + "".join(cells))
    print(f"  (reale = {real_tot:+.2f}€ su questi {n} trade)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
