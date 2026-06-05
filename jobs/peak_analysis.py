"""Analisi 'profit non protetto' sui trade Sprint 2.

Per ogni trade chiuso estrae le candele storiche fini (MINUTE_5, fallback
MINUTE_15 se la finestra eccede il cap di 1000 candele) dall'apertura alla
chiusura e ricostruisce il PEAK profit flottante realmente raggiunto
durante la vita del trade.

Peak 'incassabile' = prezzo a cui il broker avrebbe chiuso al meglio:
  - LONG  -> max(highPrice.bid)   (un long si chiude al bid)
  - SHORT -> min(lowPrice.ask)    (uno short si chiude all'ask)

Rischio (1R) = |entry - SL_originale| * size, con SL_originale derivato
dalla percentuale stop_loss del signal (il current_sl del trade e' gia'
trailato e non rappresenta il rischio all'ingresso).

Uso (lato VM, serve service_role key + sessione Capital):
    PYTHONPATH=$HOME/tradealert .venv/bin/python jobs/peak_analysis.py
    ... --json   per output JSON puro
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from src.config import load_config
from src.capital_client import CapitalClient
from src.db import Database

# Perimetro Sprint 2: id 38-55 (18 trade, fix bidirezionale in produzione
# dal 2026-05-20, primo trade id 38 / signal 85).
ID_MIN, ID_MAX = 38, 55

EPIC = {
    "Gold": "GOLD",
    "Brent Oil": "OIL_BRENT",
    "US500": "US500",
    "Nasdaq 100": "US100",
    "Bitcoin": "BTCUSD",
}


def _utc_naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _fetch_candles(cl: CapitalClient, epic: str, frm: datetime, to: datetime,
                   resolution: str, cap: int = 1000) -> list[dict]:
    params = {
        "resolution": resolution,
        "from": frm.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": to.strftime("%Y-%m-%dT%H:%M:%S"),
        "max": cap,
    }
    r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                        params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("prices", [])


def _peak_for_trade(cl: CapitalClient, trade: dict, signal: dict) -> dict:
    asset = trade["asset"]
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    size = float(trade["size"])
    tp = float(trade["current_tp"]) if trade.get("current_tp") else None
    final_pnl = float(trade["pnl"]) if trade.get("pnl") is not None else None
    epic = EPIC.get(asset)

    opened = _utc_naive(trade["opened_at"])
    closed = _utc_naive(trade["closed_at"])

    # SL originale (1R) dalla percentuale del signal; fallback su current_sl
    # se manca il signal (orphan/manuale).
    sl_pct = None
    tp_pct = None
    if signal:
        if signal.get("stop_loss") is not None:
            sl_pct = float(signal["stop_loss"])
        if signal.get("take_profit") is not None:
            tp_pct = float(signal["take_profit"])
    if sl_pct and sl_pct > 0:
        risk_dist = entry * sl_pct / 100.0
    else:
        # fallback: usa lo SL salvato sul trade (puo' essere trailato -> meno preciso)
        risk_dist = abs(entry - float(trade["current_sl"])) if trade.get("current_sl") else None

    # Finestra candele: MINUTE_5 di default. Capital rifiuta con 400 le
    # finestre troppo ampie per la granularita' fine e satura a 1000
    # candele: in entrambi i casi ripieghiamo su MINUTE_15 (copre ~10
    # giorni a 1000 candele).
    resolution = "MINUTE_5"
    try:
        candles = _fetch_candles(cl, epic, opened, closed, resolution)
        if len(candles) >= 1000:
            raise ValueError("cap raggiunto")
    except Exception:
        resolution = "MINUTE_15"
        candles = _fetch_candles(cl, epic, opened, closed, resolution)

    # Filtro stretto alla vita del trade.
    sel = []
    for c in candles:
        t = datetime.fromisoformat(c["snapshotTimeUTC"])
        if opened <= t <= closed:
            sel.append(c)
    if not sel:
        sel = candles  # fallback: usa tutto cio' che e' tornato

    # Peak incassabile.
    if direction == "long":
        peak_price = max(float(c["highPrice"]["bid"]) for c in sel)
        peak_move = peak_price - entry
    else:
        peak_price = min(float(c["lowPrice"]["ask"]) for c in sel)
        peak_move = entry - peak_price

    peak_usd = peak_move * size
    peak_R = (peak_usd / (risk_dist * size)) if (risk_dist and risk_dist > 0) else None

    reward_dist = abs(tp - entry) if tp is not None else None
    peak_pct_to_tp = (peak_move / reward_dist * 100.0) if (reward_dist and reward_dist > 0) else None
    rr = (reward_dist / risk_dist) if (reward_dist and risk_dist and risk_dist > 0) else None

    give_back_usd = (peak_usd - final_pnl) if final_pnl is not None else None
    give_back_pct = (give_back_usd / peak_usd * 100.0) if (give_back_usd is not None and peak_usd > 0) else None

    return {
        "id": trade["id"],
        "asset": asset,
        "dir": direction,
        "rr": rr,
        "peak_usd": peak_usd,
        "peak_R": peak_R,
        "peak_pct_to_tp": peak_pct_to_tp,
        "final_usd": final_pnl,
        "give_back_usd": give_back_usd,
        "give_back_pct": give_back_pct,
        "exit_reason": trade.get("exit_reason"),
        "resolution": resolution,
        "n_candles": len(sel),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    rows = (db._client.table("trades").select("*")
            .gte("id", ID_MIN).lte("id", ID_MAX)
            .eq("status", "closed").order("id").execute().data)

    out = []
    for t in rows:
        sig = None
        if t.get("signal_id") is not None:
            try:
                sig = db.get_signal(t["signal_id"])
            except Exception:
                sig = None
        try:
            out.append(_peak_for_trade(cl, t, sig))
        except Exception as exc:
            out.append({"id": t["id"], "asset": t.get("asset"), "error": str(exc)})
        time.sleep(0.6)  # gentile col rate limit Capital

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
