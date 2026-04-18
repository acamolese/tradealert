"""Entry point del position monitor.

Schedulato ogni 30 min in orario mercato. Per ogni posizione aperta,
chiede al LLM se vale la pena tenere o chiudere. Notifica Telegram solo
se l'LLM propone CLOSE. Se silenzio, vuol dire HOLD.
"""

from __future__ import annotations

import logging
import sys
import traceback

from src.config import load_config
from src.position_monitor import monitor_positions
from src.telegram_client import TelegramClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        monitor_positions(config)
        return 0
    except Exception as exc:
        logging.exception("Position monitor fallito")
        try:
            err = (
                str(exc)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            TelegramClient(config).send_message(
                f"🚨 <b>Position monitor errore</b>\n<pre>{err}</pre>"
            )
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
