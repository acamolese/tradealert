"""Entry point del job mattutino: lo invoca il workflow GitHub Actions."""

from __future__ import annotations

import logging
import sys
import traceback

from src.config import load_config
from src.scanner import run_morning_scan
from src.telegram_client import TelegramClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        run_morning_scan(config)
        return 0
    except Exception as exc:
        logging.exception("Morning scan fallito")
        try:
            err_text = (
                str(exc)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            TelegramClient(config).send_message(
                f"🚨 <b>Morning scan errore</b>\n<pre>{err_text}</pre>\n"
                f"<i>Vedi log GitHub Actions.</i>"
            )
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
