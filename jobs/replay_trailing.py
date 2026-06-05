"""Smoke test storico: replay del trailing stop su candele Sprint 2.

Simula candela-per-candela (MINUTE_5, fallback MINUTE_15) l'evoluzione di
ogni trade chiuso sotto 4 policy di trailing diverse e calcola il P&L
ipotetico di ciascuna:

  - baseline : trailing attuale (step_r=0.5; 0.5R->half, 1R->BE, poi +0.5R
               ogni 0.5R sopra 1R). Riproduce il comportamento in prod.
  - A        : R-multiple granulare a step 0.25R (chiude il buco 1.0-1.5R).
  - B        : TP-aware, lock in frazione del cammino entry->TP.
  - C        : ibrido, lock = max(A, B) (il piu' protettivo dei due).

IMPORTANTE / metodologia. Il replay assume "nessun intervento manuale":
l'uscita e' decisa solo da SL/TP automatici. Quindi il P&L baseline del
replay NON coincide col realizzato reale dei trade chiusi a mano dal
monitor; il confronto corretto e' baseline-replay vs fix-replay, che
isola l'effetto del solo cambio di trailing a parita' di ogni altra cosa.

Convenzioni:
  - long: prezzi valutati sul bid (un long si chiude al bid); SL/TP su bid.
  - short: prezzi valutati sull'ask (uno short si chiude all'ask).
  - hit nella stessa candela: si testa lo SL PRIMA del TP (conservativo)
    e con lo SL corrente PRIMA di aggiornarlo col profit della candela.
  - se ne' SL ne' TP vengono colpiti, uscita alla close dell'ultima
    candela della vita reale del trade (proxy della chiusura osservata).

Read-only: legge candele da Capital e dati da Supabase, non scrive nulla.

Uso (lato VM):
    PYTHONPATH=$HOME/tradealert .venv/bin/python jobs/replay_trailing.py
    ... --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone

from src.config import load_config
from src.capital_client import CapitalClient
from src.db import Database

ID_MIN, ID_MAX = 38, 57  # tutti i trade chiusi Sprint 2 disponibili

EPIC = {
    "Gold": "GOLD", "Brent Oil": "OIL_BRENT", "US500": "US500",
    "Nasdaq 100": "US100", "Bitcoin": "BTCUSD",
}


def _utc_naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _fetch(cl, epic, frm, to, resolution):
    params = {"resolution": resolution,
              "from": frm.strftime("%Y-%m-%dT%H:%M:%S"),
              "to": to.strftime("%Y-%m-%dT%H:%M:%S"), "max": 1000}
    r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                        params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("prices", [])


def _candles(cl, epic, opened, closed):
    try:
        c = _fetch(cl, epic, opened, closed, "MINUTE_5")
        if len(c) >= 1000:
            raise ValueError("cap")
    except Exception:
        c = _fetch(cl, epic, opened, closed, "MINUTE_15")
    sel = [x for x in c if opened <= datetime.fromisoformat(x["snapshotTimeUTC"]) <= closed]
    return sel or c


# ---- policy: ritornano l'OFFSET in unita' di R rispetto all'entry, oppure
# None se la policy non sposta ancora lo SL (resta a -1R = sl0). Long e short
# usano lo stesso offset (segno gestito dal chiamante). -------------------

def _offset_baseline(profit_r, frac_tp):
    if profit_r < 0.5:
        return None
    if profit_r < 1.0:
        return -0.5
    return math.floor((profit_r - 1.0) / 0.5) * 0.5


def _offset_A(profit_r, frac_tp):
    # step 0.25R, sia in zona rischio (0.5-1R) sia sopra il BE.
    if profit_r < 0.5:
        return None
    if profit_r < 1.0:
        # 0.5R -> -0.5 ; 0.75R -> -0.25
        return -0.5 + math.floor((profit_r - 0.5) / 0.25) * 0.25
    # 1R -> 0 (BE) ; 1.25R -> +0.25 ; 1.5R -> +0.5 ...
    return math.floor((profit_r - 1.0) / 0.25) * 0.25


def _offset_B(profit_r, frac_tp):
    # lock in frazione del cammino entry->TP, convertito in unita' di R a
    # valle dal chiamante (qui ritorniamo direttamente l'offset in R).
    # frac_tp = quota di cammino verso il TP raggiunta al peak della candela.
    # Sotto 30% del TP: nessun lock (resta sl0). Soglie crescenti.
    if frac_tp < 0.30:
        return None
    if frac_tp < 0.50:
        return 0.0            # breakeven
    if frac_tp < 0.70:
        lock_frac = 0.15
    elif frac_tp < 0.85:
        lock_frac = 0.35
    else:
        lock_frac = 0.55
    # offset in R = lock_frac * (TP-entry)/risk = lock_frac * rr
    return ("FRAC", lock_frac)


def _offset_B_late(profit_r, frac_tp):
    # Variante TP-aware "tardiva": aggancia il profit solo molto vicino al
    # TP, per non tagliare i mid-runner che ritracciano dal 50-70%.
    if frac_tp < 0.80:
        return None
    if frac_tp < 0.90:
        return ("FRAC", 0.45)
    return ("FRAC", 0.65)


def _simulate(trade, signal, candles):
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    size = float(trade["size"])
    tp = float(trade["current_tp"])
    sl_pct = float(signal["stop_loss"]) if signal and signal.get("stop_loss") else None
    if sl_pct and sl_pct > 0:
        risk = entry * sl_pct / 100.0
    else:
        risk = abs(entry - float(trade["current_sl"]))
    rr = abs(tp - entry) / risk if risk else 0.0
    long = direction == "long"

    def price_hi(c):  # estremo favorevole della candela
        return float(c["highPrice"]["bid"]) if long else float(c["lowPrice"]["ask"])

    def price_lo(c):  # estremo avverso della candela
        return float(c["lowPrice"]["bid"]) if long else float(c["highPrice"]["ask"])

    def offset_to_sl(off):
        if off is None:
            off = -1.0  # sl0
        if isinstance(off, tuple):  # ("FRAC", lock_frac) -> in unita' di R
            off = off[1] * rr
        return entry + off * risk if long else entry - off * risk

    def sl_from(off):
        """SL assoluto da un offset di policy (None / float-R / FRAC tuple)."""
        return offset_to_sl(off)

    def run(policies):
        """policies: lista di funzioni offset(profit_r, frac_tp). Lo SL preso
        e' il piu' protettivo tra quelli proposti. Una sola funzione = policy
        semplice; piu' funzioni = ibrido."""
        sl = offset_to_sl(-1.0)  # sl0 = -1R
        for c in candles:
            hi, lo = price_hi(c), price_lo(c)
            adverse = lo if long else hi  # prezzo che puo' colpire lo SL
            favor = hi if long else lo    # prezzo che puo' colpire il TP
            # 1. hit SL corrente (conservativo: prima dello SL update)
            if (long and adverse <= sl) or ((not long) and adverse >= sl):
                return (sl, "SL", (sl - entry) * size if long else (entry - sl) * size)
            # 2. hit TP
            if (long and favor >= tp) or ((not long) and favor <= tp):
                return (tp, "TP", (tp - entry) * size if long else (entry - tp) * size)
            # 3. aggiorna trailing col profit all'estremo favorevole
            profit_r = ((hi - entry) if long else (entry - lo)) / risk
            frac_tp = ((hi - entry) if long else (entry - lo)) / abs(tp - entry)
            cand = sl
            for pol in policies:
                s = sl_from(pol(profit_r, frac_tp))
                if (long and s > cand) or ((not long) and s < cand):
                    cand = s
            sl = cand
        last = candles[-1]
        cl_px = float(last["closePrice"]["bid"]) if long else float(last["closePrice"]["ask"])
        return (cl_px, "EOD", (cl_px - entry) * size if long else (entry - cl_px) * size)

    plans = {
        "baseline": [_offset_baseline],
        "A": [_offset_A],
        "B": [_offset_B],
        "C": [_offset_A, _offset_B],            # ibrido aggressivo
        "D": [_offset_A, _offset_B_late],       # ibrido con TP-lock tardivo
    }
    res = {}
    for name, pols in plans.items():
        exit_px, reason, pnl = run(pols)
        res[name] = {"exit": round(exit_px, 4), "reason": reason, "pnl": round(pnl, 4)}
    res["_meta"] = {"rr": round(rr, 3), "risk": round(risk, 5),
                    "real_pnl": trade.get("pnl"), "real_exit": trade.get("exit_reason")}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config(); db = Database(cfg); cl = CapitalClient(cfg); cl.login()
    rows = (db._client.table("trades").select("*")
            .gte("id", ID_MIN).lte("id", ID_MAX).eq("status", "closed")
            .order("id").execute().data)
    out = []
    for t in rows:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        try:
            cands = _candles(cl, EPIC[t["asset"]], _utc_naive(t["opened_at"]),
                             _utc_naive(t["closed_at"]))
            r = _simulate(t, sig, cands)
            r["id"] = t["id"]; r["asset"] = t["asset"]; r["dir"] = t["direction"]
            r["n_candles"] = len(cands)
            out.append(r)
        except Exception as exc:
            out.append({"id": t["id"], "asset": t["asset"], "error": str(exc)})
        time.sleep(0.6)
    # --- sintesi aggregata ---
    pols = ["baseline", "A", "B", "C", "D"]
    ok = [r for r in out if "error" not in r]
    def total(pol, ids=None):
        return round(sum(r[pol]["pnl"] for r in ok
                         if ids is None or r["id"] in ids), 4)
    focus = {45, 51, 55}
    summary = {
        "n_trades": len(ok),
        "perimetro_38_57": {p: total(p) for p in pols},
        "perimetro_38_55": {p: total(p, set(range(38, 56))) for p in pols},
        "focus_45_51_55": {p: total(p, focus) for p in pols},
        "real_pnl_38_57": round(sum(float(r["_meta"]["real_pnl"] or 0) for r in ok), 4),
    }
    print(json.dumps(out, indent=2, default=str))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
