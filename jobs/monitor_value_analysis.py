"""Backtest controfattuale: il monitor LLM aggiunge o sottrae valore?

Per i trade chiusi manualmente su suggerimento del monitor (exit_reason
che inizia con 'manual'), ricostruisce il P&L IPOTETICO che si sarebbe
ottenuto lasciando correre la posizione fino a TP o SL naturale.

Metodo:
- counterfactual a partire dall'istante di chiusura reale (closed_at);
- replica la logica di trailing stop di _apply_trailing_stop
  (profit < 0.5R: SL originale; 0.5-1R: half-risk; >=1R: ladder 0.5R);
- candele orarie da Capital, prezzo mid (bid+ask)/2;
- TP colpito se high >= TP; SL colpito se low <= SL;
- se nella stessa candela vengono toccati entrambi, conta lo SL
  (assunzione conservativa, l'ordine intra-candela non e' noto);
- se nessuna soglia viene toccata, il trade resta aperto e si valuta
  mark-to-market sull'ultima candela.

Uso (lato VM, serve service_role key + sessione Capital):
    .venv/bin/python -m jobs.monitor_value_analysis
    .venv/bin/python -m jobs.monitor_value_analysis --trades 34,36,37
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime

from supabase import create_client

from src.capital_client import CapitalClient
from src.config import load_config

EPIC = {"Gold": "GOLD", "Brent Oil": "OIL_BRENT"}
STEP_R = 0.5


def _mid(p: dict) -> float:
    return (p["bid"] + p["ask"]) / 2


def _candle_dt(c: dict) -> datetime:
    raw = c.get("snapshotTimeUTC") or c["snapshotTime"]
    raw = raw.replace("/", "-")
    if raw.endswith("Z"):
        raw = raw[:-1]
    return datetime.fromisoformat(raw.replace(" ", "T"))


def _trailing_sl(entry: float, r_dist: float, peak: float, current_sl: float) -> float:
    """Replica _apply_trailing_stop per un long. Ritorna lo SL aggiornato
    (solo verso l'alto) dato il picco di prezzo raggiunto."""
    profit_r = (peak - entry) / r_dist
    if profit_r < 0.5:
        return current_sl
    if profit_r < 1.0:
        offset_r = -0.5
    else:
        n = math.floor((profit_r - 1.0) / STEP_R)
        offset_r = n * STEP_R
    new_sl = entry + offset_r * r_dist
    return max(current_sl, new_sl)


def simulate(trade: dict, candles: list[dict]) -> dict:
    entry = float(trade["entry_price"])
    tp = float(trade["current_tp"])
    size = float(trade["size"])
    # SL originale del signal: trade.current_sl non viene aggiornato dal
    # trailing, quindi rappresenta lo stop iniziale.
    orig_sl = float(trade["current_sl"])
    r_dist = entry - orig_sl
    closed_at = datetime.fromisoformat(trade["closed_at"]).replace(tzinfo=None)

    # Candele successive alla chiusura reale, ordinate.
    fwd = sorted(
        (c for c in candles if _candle_dt(c) >= closed_at),
        key=_candle_dt,
    )

    # Stato SL all'istante della chiusura reale: se durante la vita reale
    # il trailing aveva gia' mosso lo SL, ricostruiamo il picco pre-close.
    sl = orig_sl
    # picco raggiunto prima della chiusura -> applica il trailing iniziale
    pre = [c for c in candles if _candle_dt(c) < closed_at]
    if pre:
        peak_pre = max(_mid(c["highPrice"]) for c in pre)
        sl = _trailing_sl(entry, r_dist, peak_pre, sl)

    outcome = "OPEN"
    exit_price = None
    exit_dt = None
    for c in fwd:
        hi = _mid(c["highPrice"])
        lo = _mid(c["lowPrice"])
        if lo <= sl:
            outcome, exit_price, exit_dt = "SL", sl, _candle_dt(c)
            break
        if hi >= tp:
            outcome, exit_price, exit_dt = "TP", tp, _candle_dt(c)
            break
        sl = _trailing_sl(entry, r_dist, hi, sl)

    if exit_price is None:
        last = fwd[-1] if fwd else None
        exit_price = _mid(last["closePrice"]) if last else entry
        exit_dt = _candle_dt(last) if last else closed_at

    hyp_pnl = size * (exit_price - entry)
    return {
        "entry": entry,
        "orig_sl": orig_sl,
        "tp": tp,
        "size": size,
        "sl_at_close": sl if outcome != "SL" else exit_price,
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_dt": exit_dt,
        "hyp_pnl": hyp_pnl,
        "real_pnl": float(trade["pnl"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest valore monitor LLM")
    parser.add_argument(
        "--trades", default="34,36,37", help="ID trade separati da virgola"
    )
    args = parser.parse_args()
    ids = [int(x) for x in args.trades.split(",")]

    config = load_config()
    sb = create_client(
        config.supabase_url,
        config.supabase_service_role_key or config.supabase_anon_key,
    )
    cap = CapitalClient(config)
    cap.login()

    candle_cache: dict[str, list[dict]] = {}
    real_cum = 0.0
    hyp_cum = 0.0

    print("=== Backtest controfattuale monitor LLM ===\n")
    for tid in ids:
        rows = sb.table("trades").select("*").eq("id", tid).execute().data
        if not rows:
            print(f"#{tid}: non trovato\n")
            continue
        t = rows[0]
        epic = EPIC.get(t["asset"])
        if epic is None:
            print(f"#{tid}: epic sconosciuto per {t['asset']}\n")
            continue
        if epic not in candle_cache:
            candle_cache[epic] = cap.get_prices(epic, resolution="HOUR", max_bars=300)
        res = simulate(t, candle_cache[epic])

        real_cum += res["real_pnl"]
        hyp_cum += res["hyp_pnl"]
        delta = res["hyp_pnl"] - res["real_pnl"]
        verdict = "monitor ha SOTTRATTO" if delta > 0 else "monitor ha AGGIUNTO"
        print(
            f"#{tid} {t['asset']} {t['direction']}  size={res['size']:g}\n"
            f"  entry={res['entry']:.3f}  SL_orig={res['orig_sl']:.3f}  "
            f"TP={res['tp']:.3f}\n"
            f"  chiusura reale: close={t['close_price']}  "
            f"P&L reale = {res['real_pnl']:+.2f} EUR\n"
            f"  controfattuale: esito={res['outcome']}  "
            f"exit={res['exit_price']:.3f} @ {res['exit_dt']}  "
            f"P&L ipotetico = {res['hyp_pnl']:+.2f} EUR\n"
            f"  delta (ipotetico - reale) = {delta:+.2f} EUR  -> {verdict}\n"
        )

    diff = hyp_cum - real_cum
    print("=== CUMULATO ===")
    print(f"  Reale    (con CLOSE del monitor): {real_cum:+.2f} EUR")
    print(f"  Ipotetico (senza monitor, TP/SL): {hyp_cum:+.2f} EUR")
    if diff > 0:
        print(f"  Il monitor ha SOTTRATTO {diff:.2f} EUR di valore.")
    elif diff < 0:
        print(f"  Il monitor ha AGGIUNTO {-diff:.2f} EUR di valore.")
    else:
        print("  Il monitor e' stato neutro.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
