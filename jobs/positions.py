"""Entry point per gestire le posizioni aperte via Telegram.

Uso locale:
    python -m jobs.positions

Comportamento: lista le posizioni aperte sul conto Capital configurato,
le manda su Telegram con un bottone Chiudi per ognuna, attende il click
fino a CONFIRM_TIMEOUT_SEC secondi e poi chiude la posizione richiesta.
"""

from __future__ import annotations

import logging
import sys
import traceback

from src.config import load_config
from src.positions import manage_positions
from src.telegram_client import TelegramClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        manage_positions(config)
        return 0
    except Exception as exc:
        logging.exception("Gestione posizioni fallita")
        try:
            err = (
                str(exc)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            TelegramClient(config).send_message(
                f"🚨 <b>Errore /posizioni</b>\n<pre>{err}</pre>"
            )
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
