"""Entry point del trailing-stop leggero.

Schedulato ogni 5 min in orario mercato. Applica solo la logica di
trailing SL sulle posizioni aperte (nessuna chiamata LLM): se una
posizione ha raggiunto +0.5R, +1R o oltre, sposta lo SL al livello
appropriato (half-risk / breakeven / profit-locking).
"""

from __future__ import annotations

import logging
import sys
import traceback

from src.config import load_config
from src.position_monitor import run_trailing_stops
from src.telegram_client import TelegramClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        run_trailing_stops(config)
        return 0
    except Exception as exc:
        logging.exception("Trailing stop fallito")
        try:
            err = (
                str(exc)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            TelegramClient(config).send_message(
                f"🚨 <b>Trailing stop errore</b>\n<pre>{err}</pre>"
            )
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
