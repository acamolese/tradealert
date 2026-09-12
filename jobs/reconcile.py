"""Entry point del reconcile trade DB <-> Capital.

Cron: ogni ora. Chiude nel DB i trade che sono spariti dal broker
(SL/TP colpiti, chiusure manuali dal frontend, ecc), cercando di
recuperare close_price e P&L dalla history/activity Capital.

Dal 2026-09-12 lo stesso giro allinea anche le posizioni della strategia di
volatilita', che vive sul conto di prova e su uno schema separato: il broker
puo' chiuderla con lo stop di mercato mentre il job giornaliero dorme, e senza
questo passaggio la riga resterebbe aperta per sempre nel database.
"""

from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.reconcile import reconcile_open_trades
from src.vol_runner import riconcilia as riconcilia_volatilita


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("reconcile")
    config = load_config()
    counters = reconcile_open_trades(config)
    log.info(
        "Reconcile done: checked=%d stale=%d closed=%d with_pnl=%d",
        counters["checked"],
        counters["stale"],
        counters["closed"],
        counters["closed_with_pnl"],
    )
    try:
        vol = riconcilia_volatilita(config)
        log.info(
            "Reconcile volatilita': registrate=%d vive=%d chiuse=%d",
            vol["registrate"], vol["vive"], vol["chiuse"],
        )
    except Exception as exc:  # non deve mai far fallire il reconcile principale
        log.error("Reconcile volatilita' fallito: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
