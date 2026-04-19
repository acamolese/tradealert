"""Entry point dell'health check VM Oracle.

Schedulato una volta al giorno. Raccoglie metriche locali (RAM, disco,
uptime, banda outbound via vnstat, stato listener) e manda un summary
su Telegram. Se c'e' un warning o critico, il messaggio ha l'icona
adeguata e, in caso critico, suggerisce di fermare temporaneamente i
job per evitare costi Oracle.
"""

from __future__ import annotations

import logging
import sys
import traceback

from src.config import load_config
from src.health import build_health_report
from src.telegram_client import TelegramClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        message, _has_warning = build_health_report()
        TelegramClient(config).send_message(message)
        return 0
    except Exception as exc:
        logging.exception("Health check fallito")
        try:
            err = (
                str(exc)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            TelegramClient(config).send_message(
                f"🚨 <b>Health check errore</b>\n<pre>{err}</pre>"
            )
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
