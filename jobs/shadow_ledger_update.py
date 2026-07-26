"""Benchmark ombra v2 (spec §6.3). Gira 1x/giorno dopo il controller.

Popola shadow_ledger con TRE strategie dallo stesso feed prezzi e con gli stessi
costi modellati, cosi' il confronto contro il beta passivo e' una proprieta' dello
schema, non una buona intenzione:
  - controller       : l'esposizione reale decisa dal controller (da exposure_state)
  - always_1_block   : 1 blocco long sempre aperto, mai chiuso (il beta passivo)
  - flat             : nessuna esposizione

Metrica primaria del report (§6.3/§10): information ratio del controller CONTRO
always_1_block. Le curve sono P&L in euro sul capitale, da rendimenti percentuali
sul nozionale (non dai pnl euro del DB, inaffidabili).

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.shadow_ledger_update [--dry-run]
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from src.config import load_config
from src.exposure_config import load_exposure_config

log = logging.getLogger(__name__)

FINANCING_DAILY = 0.00025   # 0.025%/notte sul nozionale (non-crypto), come il backtest
STRATEGIES = ("controller", "always_1_block", "flat")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv

    from src.capital_client import CapitalClient
    from src.db import Database
    from src.executor import _market_meta
    from src.leverage import real_leverage

    v1 = load_config()
    cfg = load_exposure_config()
    db = Database(v1)
    capital = CapitalClient(v1)
    capital.login()

    market = capital.get_market(cfg.epic)
    meta = _market_meta(market, leverages_map=capital.get_leverages_map(), use_real_leverage=True)
    L = real_leverage(cfg.epic) or (1.0 / meta["margin_factor"] if meta["margin_factor"] else 20.0)
    notional_block = cfg.block_margin_eur * L            # nozionale di 1 blocco
    snap = market.get("snapshot", {}) or {}
    bid, ask = snap.get("bid"), snap.get("offer")
    spread_frac = ((float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2)) if bid and ask else 0.0002

    # rendimento giornaliero US500 (dalle ultime 2 candele DAY)
    prices = capital.get_prices(cfg.epic, resolution="DAY", max_bars=3)
    def mid(p):
        cp = p.get("closePrice") or {}
        return (float(cp.get("bid")) + float(cp.get("ask"))) / 2 if cp.get("bid") and cp.get("ask") else None
    closes = [mid(p) for p in prices if mid(p)]
    if len(closes) < 2:
        log.warning("meno di 2 candele DAY: skip ledger.")
        return 0
    ret = closes[-1] / closes[-2] - 1.0

    today = datetime.now(timezone.utc).date().isoformat()

    # esposizione del controller oggi (blocchi correnti da exposure_state, fallback 0)
    st = (db._client.table("exposure_state").select("blocks_current,blocks_target")
          .eq("epic", cfg.epic).order("as_of_date", desc=True).limit(1).execute().data)
    blocks_ctrl = int(st[0]["blocks_current"]) if st else 0

    exposures = {
        "controller": blocks_ctrl * notional_block,
        "always_1_block": notional_block,
        "flat": 0.0,
    }

    # capitale base: equity reale iniziale del ledger, o l'equity attuale se primo giorno
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    equity_now = float((acc.get("balance") or {}).get("balance") or 0.0)

    rows = []
    for strat in STRATEGIES:
        prev = (db._client.table("shadow_ledger").select("equity_eur,cum_return,exposure_eur")
                .eq("strategy", strat).order("as_of_date", desc=True).limit(1).execute().data)
        base_equity = float(prev[0]["equity_eur"]) if prev else equity_now
        prev_exposure = float(prev[0]["exposure_eur"]) if prev else 0.0
        prev_cum = float(prev[0]["cum_return"]) if prev else 0.0

        exp = exposures[strat]
        gross = exp * ret                                  # P&L lordo del giorno
        financing = exp * FINANCING_DAILY
        turnover_cost = abs(exp - prev_exposure) * spread_frac  # spread sul cambio di esposizione
        cost = financing + turnover_cost
        pnl = gross - cost
        equity = base_equity + pnl
        daily_ret = pnl / base_equity if base_equity else 0.0
        cum = (1 + prev_cum) * (1 + daily_ret) - 1
        rows.append({
            "as_of_date": today, "strategy": strat,
            "equity_eur": round(equity, 4), "exposure_eur": round(exp, 2),
            "daily_return": round(daily_ret, 6), "cum_return": round(cum, 6),
            "cost_modeled": round(cost, 4),
        })

    print(f"=== shadow_ledger {today} | ret US500 {ret:+.3%} | nozionale/blocco {notional_block:.0f}€ ===")
    for r in rows:
        print(f"  {r['strategy']:<16} exp {r['exposure_eur']:>6.0f}€ "
              f"day {r['daily_return']:+.3%} cum {r['cum_return']:+.3%} cost {r['cost_modeled']:.3f}€")

    if dry:
        print("(dry-run: non scritto)")
        return 0
    for r in rows:
        db._client.table("shadow_ledger").upsert(r, on_conflict="as_of_date,strategy").execute()
    log.info("shadow_ledger aggiornato (%d strategie)", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
