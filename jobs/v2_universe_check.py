"""Quanti strumenti del tabellone sono ESEGUIBILI da v2 sul conto REALE?

Prerequisito allo switch dinamico dello strumento: v2 apre blocchi da
BLOCK_MARGIN_EUR di margine, e rifiuta uno strumento se la taglia minima del
broker costa piu' di un blocco (§2.1). Il tabellone gira su DEMO con equity 100;
il conto reale ha equity ~58 e leve per-strumento diverse. Se solo US500 passa,
collegare v2 al tabellone non produce nessun dinamismo e non va fatto.

Legge il conto reale in sola lettura: nessun ordine, nessuna scrittura.
Uso: python -m jobs.v2_universe_check [n_candidati]
"""
from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.db import Database
from src.exposure_config import load_exposure_config
from src.capital_client import CapitalClient
from src.executor import _market_meta
from src.leverage import real_leverage
from src.risk import quote_to_ref_factor
from src.exposure_controller import compute_n_max

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    topn = int(sys.argv[1]) if len(sys.argv) > 1 else 25

    v1 = load_config()
    cfg = load_exposure_config()
    db = Database(v1)
    sp = db._client.schema("spinner")

    last = (sp.table("odds_board").select("scan_date")
            .order("scan_date", desc=True).limit(1).execute().data)[0]["scan_date"]
    board = (sp.table("odds_board").select("*")
             .eq("scan_date", last).eq("status", "eligible")
             .order("g_exec", desc=True).limit(topn).execute().data)

    capital = CapitalClient(v1)
    capital.login()
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    equity = float(bal.get("balance") or 0.0) + float(bal.get("profitLoss") or 0.0)
    lev_map = capital.get_leverages_map()

    print(f"Tabellone {last}: {len(board)} candidati (top {topn} per g_exec)")
    print(f"Conto REALE: equity {equity:.2f}€ | blocco {cfg.block_margin_eur:.0f}€ "
          f"| gap tolerance {cfg.gap_tolerance}\n")
    print(f"{'epic':<10} {'side':<5} {'g_dem':>6} {'minsz':>8} {'prezzo':>10} "
          f"{'lev':>5} {'min€':>8} {'N_max':>6}  esito")

    ok = 0
    for r in board:
        epic = r["epic"]
        try:
            market = capital.get_market(epic)
        except Exception as exc:
            print(f"{epic:<10} {r['side']:<5} {'':>6} {'':>8} {'':>10} {'':>5} "
                  f"{'':>8} {'':>6}  market non leggibile ({type(exc).__name__})")
            continue
        instr = market.get("instrument", {}) or {}
        meta = _market_meta(market, leverages_map=lev_map, use_real_leverage=True)
        mid, mf, mn = meta["mid_price"], meta["margin_factor"], meta["min_size"]
        if not mid or not mf:
            print(f"{epic:<10} {r['side']:<5} {'':>6} {'':>8} {'':>10} {'':>5} "
                  f"{'':>8} {'':>6}  prezzo/margin factor assente")
            continue
        q2r = quote_to_ref_factor(instr.get("currency"), capital) or 1.0
        lev = real_leverage(epic) or (1.0 / mf)
        min_margin = mn * mid * mf * q2r
        n_max = compute_n_max(equity, cfg.block_margin_eur, lev, cfg.gap_tolerance)
        if min_margin <= cfg.block_margin_eur:
            esito, ok = "ESEGUIBILE", ok + 1
        else:
            need = min_margin * (1.0 + lev * cfg.gap_tolerance)
            esito = (f"no: 1 taglia min = {min_margin:.1f}€ "
                     f"(servirebbe blocco {min_margin:.1f}€, equity {need:.0f}€)")
        print(f"{epic:<10} {r['side']:<5} {float(r['g_exec'] or 0)*100:>5.1f}% "
              f"{mn:>8.4g} {mid:>10.2f} {lev:>5.0f} {min_margin:>8.2f} {n_max:>6}  {esito}")

    print(f"\nEseguibili sul reale a blocchi da {cfg.block_margin_eur:.0f}€: "
          f"{ok}/{len(board)}")
    if ok <= 1:
        print("=> Lo switch dinamico dello strumento NON produce dinamismo: "
              "il vincolo di taglia minima lascia una sola scelta.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
