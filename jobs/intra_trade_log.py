"""Entry point: logging high/low intra-trade delle posizioni aperte.

Registra su monitoring_events l'evento ``intra_trade_extreme`` con gli
estremi cumulativi (high_seen / low_seen) dall'apertura. Read + append
only, nessun impatto sul trailing live.

Da schedulare ~30m (allineato al position monitor) DOPO la chiusura
formale Sprint 2. Esempio crontab:
    */30 * * * *  cd $TRADEALERT_HOME && python -m jobs.intra_trade_log \
        >> logs/intra_trade.log 2>&1

Uso locale:
    python -m jobs.intra_trade_log
"""

from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.intra_trade import record_intra_trade_extremes


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    n = record_intra_trade_extremes(config)
    logging.getLogger(__name__).info("intra_trade: %d eventi scritti", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
