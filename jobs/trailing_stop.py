"""Entry point del trailing-stop leggero.

Schedulato ogni 5 min in orario mercato. Applica solo la logica di
trailing SL sulle posizioni aperte (nessuna chiamata LLM): se una
posizione ha raggiunto +0.5R, +1R o oltre, sposta lo SL al livello
appropriato (half-risk / breakeven / profit-locking).
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

from src.config import load_config
from src.position_monitor import run_trailing_stops
from src.telegram_client import TelegramClient

# Un singolo timeout transitorio verso Capital (host dietro Imperva) si
# auto-recupera al ciclo successivo (5 min) e NON lascia scoperte le posizioni:
# gli SL sono server-side su Capital. Quindi non allarmiamo sul primo blip:
# l'alert Telegram scatta solo dopo ALERT_AFTER fallimenti CONSECUTIVI
# (15 min = problema reale, non rumore). Stato su file.
_FAIL_STATE = os.path.expanduser("~/tradealert/logs/.trailing_fail_count")
ALERT_AFTER = 3


def _read_fails() -> int:
    try:
        with open(_FAIL_STATE) as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _write_fails(n: int) -> None:
    try:
        os.makedirs(os.path.dirname(_FAIL_STATE), exist_ok=True)
        with open(_FAIL_STATE, "w") as f:
            f.write(str(n))
    except Exception:
        pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        run_trailing_stops(config)
        _write_fails(0)  # successo: azzera il contatore
        return 0
    except Exception as exc:
        fails = _read_fails() + 1
        _write_fails(fails)
        logging.exception("Trailing stop fallito (%d consecutivi)", fails)
        if fails >= ALERT_AFTER:
            try:
                err = (
                    str(exc)
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                TelegramClient(config).send_message(
                    f"🚨 <b>Trailing stop: Capital irraggiungibile da "
                    f"{fails} cicli</b> (~{fails * 5} min)\n"
                    f"Le posizioni restano protette dagli SL server-side. "
                    f"Controllo connettivita' broker.\n<pre>{err}</pre>"
                )
            except Exception:
                traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
