"""Analisi retrospettiva metrica 'chasing' (estensione del movimento all'entry).

Ricalcola, sui trade chiusi 38-61, due metriche di "inseguimento" del
movimento alla direzione del trade, per validare se il segnale visto su
daily_pct_change regge con la normalizzazione in ATR:

  chasing_daily      = daily_pct_change firmato per direzione
                       (long: +dpc ; short: -dpc). Alto = entro dopo un
                       movimento gia' ampio nella mia direzione.

  chasing_atr_swing  = distanza in ATR dal livello di swing (proxy del
                       trigger del setup), firmata:
                       long:  (last_price - low_20)  / atr_4h
                       short: (high_20    - last_price) / atr_4h
                       Alto = il prezzo si e' gia' mosso di molti ATR dal
                       punto di svolta dello swing prima dell'entry.

  chasing_atr_daily  = dpc firmato / atr_pct_of_price. Versione ATR del
                       proxy daily (il movimento di oggi normalizzato per
                       la volatilita' invece che per il prezzo). Riportata
                       per confronto.

peak_R (proxy ex-post del fatto che il trade sia mai andato a favore):
  - id 38-58: valori gia' ricostruiti in docs/sprint3.5-entry-diagnosis.md
    (candele 5m/15m). Riusati qui (PEAK_R_DOC).
  - id 59-61: ricalcolati dall'evento intra_trade_extreme nativo
    (high_seen/low_seen cumulativi) + 1R dallo stop_loss% del signal.

Sola lettura, nessuna modifica al sistema. L'anon key basta (RLS consente
le select). Uso:
    PYTHONPATH=$HOME/tradealert .venv/bin/python jobs/chasing_analysis.py
"""

from __future__ import annotations

import json
import sys

from src.config import load_config
from src.db import Database

ID_MIN, ID_MAX = 38, 61

# peak_R ricostruito in docs/sprint3.5-entry-diagnosis.md (sample 38-58).
PEAK_R_DOC = {
    38: 0.28, 39: 0.38, 40: 1.12, 41: 0.13, 42: 2.93, 43: 0.52, 44: 0.68,
    45: 1.27, 46: 0.45, 47: -0.09, 48: 1.41, 49: 2.17, 50: 0.11, 51: 1.16,
    52: 2.08, 53: 0.08, 54: 0.77, 55: 1.09, 56: 0.05, 57: 0.90, 58: 2.10,
}


def _peak_r_from_intra(db: Database, trade: dict, sig: dict | None) -> float | None:
    """peak_R per i trade Sprint 3+ dall'ultimo intra_trade_extreme."""
    ev = db.get_last_monitoring_event(trade["id"], "intra_trade_extreme")
    if not ev:
        return None
    det = ev.get("details") or {}
    high_seen = det.get("high_seen")
    low_seen = det.get("low_seen")
    entry = float(trade["entry_price"])
    sl_pct = float(sig["stop_loss"]) if sig and sig.get("stop_loss") else None
    if not sl_pct or sl_pct <= 0:
        return None
    risk = entry * sl_pct / 100.0
    if trade["direction"] == "long" and high_seen is not None:
        return round((float(high_seen) - entry) / risk, 2)
    if trade["direction"] == "short" and low_seen is not None:
        return round((entry - float(low_seen)) / risk, 2)
    return None


def _signed(value: float, direction: str) -> float:
    return value if direction == "long" else -value


def _chasing(feat: dict, direction: str) -> dict:
    last = feat.get("last_price")
    atr = feat.get("atr_4h")
    hi = feat.get("high_20")
    lo = feat.get("low_20")
    dpc = feat.get("daily_pct_change")
    atr_pct = feat.get("atr_pct_of_price")

    out: dict[str, float | None] = {
        "chasing_daily": None,
        "chasing_atr_swing": None,
        "chasing_atr_daily": None,
    }
    if dpc is not None:
        out["chasing_daily"] = round(_signed(float(dpc), direction), 3)
    if dpc is not None and atr_pct:
        out["chasing_atr_daily"] = round(_signed(float(dpc) / float(atr_pct), direction), 3)
    if last is not None and atr and hi is not None and lo is not None:
        if direction == "long":
            raw = (float(last) - float(lo)) / float(atr)
        else:
            raw = (float(hi) - float(last)) / float(atr)
        out["chasing_atr_swing"] = round(raw, 3)  # gia' "firmato" per costruzione
    return out


def main() -> int:
    cfg = load_config()
    db = Database(cfg)

    trades = (db._client.table("trades").select("*")
              .gte("id", ID_MIN).lte("id", ID_MAX)
              .eq("status", "closed").order("id").execute().data)

    rows = []
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        feat = (sig or {}).get("features_at_decision") or {}
        ch = _chasing(feat, t["direction"])
        peak = PEAK_R_DOC.get(t["id"])
        if peak is None:
            peak = _peak_r_from_intra(db, t, sig)
        exit_reason = (t.get("exit_reason") or "")
        rows.append({
            "id": t["id"],
            "asset": t["asset"],
            "dir": t["direction"],
            "pnl": round(float(t["pnl"]), 2) if t.get("pnl") is not None else None,
            "peak_R": peak,
            "full_tp": "tp" in exit_reason.lower(),
            "chasing_daily": ch["chasing_daily"],
            "chasing_atr_swing": ch["chasing_atr_swing"],
            "chasing_atr_daily": ch["chasing_atr_daily"],
            "exit_reason": exit_reason[:30],
        })

    # Ordina per chasing_atr_swing decrescente (None in fondo).
    rows_sorted = sorted(
        rows, key=lambda r: (r["chasing_atr_swing"] is None,
                             -(r["chasing_atr_swing"] or 0)))

    hdr = f"{'id':>3} {'asset':<10} {'dir':<5} {'pnl':>7} {'peakR':>6} {'TP':>3} {'ch_daily':>9} {'ch_atr_sw':>10} {'ch_atr_d':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows_sorted:
        print(f"{r['id']:>3} {r['asset']:<10} {r['dir']:<5} "
              f"{(r['pnl'] if r['pnl'] is not None else 0):>7.2f} "
              f"{(r['peak_R'] if r['peak_R'] is not None else 0):>6.2f} "
              f"{'Y' if r['full_tp'] else '':>3} "
              f"{(r['chasing_daily'] if r['chasing_daily'] is not None else 0):>9.3f} "
              f"{(r['chasing_atr_swing'] if r['chasing_atr_swing'] is not None else 0):>10.3f} "
              f"{(r['chasing_atr_daily'] if r['chasing_atr_daily'] is not None else 0):>9.3f}")

    # --- Controlli di robustezza richiesti dal task ---
    print("\n=== ROBUSTEZZA ===")
    full_tps = [r for r in rows if r["full_tp"]]
    wins = [r for r in rows if (r["pnl"] or 0) > 0]
    print(f"Full TP (exit tp_hit): {[r['id'] for r in full_tps]}")
    print(f"WIN (pnl>0): {[r['id'] for r in wins]}")

    def stat(metric):
        vals = [(r["id"], r[metric], r["pnl"], r["peak_R"], r["full_tp"])
                for r in rows if r[metric] is not None]
        vals.sort(key=lambda x: -x[1])
        return vals

    for metric in ["chasing_daily", "chasing_atr_swing", "chasing_atr_daily"]:
        vals = stat(metric)
        n = len(vals)
        top5 = vals[:5]
        bot5 = vals[-5:]
        top5_loss = sum(1 for v in top5 if (v[2] or 0) <= 0)
        # posizione dei full TP nella classifica (1 = piu' inseguitore)
        tp_ranks = {v[0]: i + 1 for i, v in enumerate(vals)}
        tp_pos = {r["id"]: tp_ranks.get(r["id"]) for r in full_tps if r["id"] in tp_ranks}
        max_tp_chasing = max((v[1] for v in vals if v[4]), default=None)
        print(f"\n[{metric}] n={n}")
        print(f"  top5 (piu' inseguitori): {[(v[0], round(v[1],2), v[2]) for v in top5]}  -> loss in top5: {top5_loss}/5")
        print(f"  bot5 (meno inseguitori): {[(v[0], round(v[1],2), v[2]) for v in bot5]}")
        print(f"  rank dei full TP (su {n}, 1=piu' inseguitore): {tp_pos}")
        print(f"  max chasing tra i full TP: {round(max_tp_chasing,3) if max_tp_chasing is not None else None}")

    print("\n=== JSON ===")
    print(json.dumps(rows_sorted, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
