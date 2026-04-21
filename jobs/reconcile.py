"""Entry point del reconcile trade DB <-> Capital.

Cron: ogni ora. Chiude nel DB i trade che sono spariti dal broker
(SL/TP colpiti, chiusure manuali dal frontend, ecc), cercando di
recuperare close_price e P&L dalla history/activity Capital.
"""

from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.reconcile import reconcile_open_trades


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
