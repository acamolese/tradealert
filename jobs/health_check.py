"""Entry point dell'health check VM Oracle.

Schedulato una volta al giorno. Raccoglie metriche locali (RAM, disco,
uptime, banda outbound via vnstat, stato listener) e manda un summary
su Telegram. Se c'e' un warning o critico, il messaggio ha l'icona
adeguata e, in caso critico, suggerisce di fermare temporaneamente i
job per evitare costi Oracle.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config
from src.health import build_health_report
from src.telegram_client import TelegramClient

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    try:
        message, has_warning = build_health_report()
        # Dal 2026-09-03: silenzio quando e' tutto regolare (il messaggio del
        # mattino riporta "Sistema: tutto regolare"); messaggio dedicato solo
        # se c'e' un avviso. L'esito resta su file per il buongiorno.
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            (DATA / "health_last.json").write_text(json.dumps(
                {"ok": not has_warning, "quando": datetime.now(timezone.utc).isoformat()}))
        except Exception:
            logging.exception("scrittura health_last fallita")
        if has_warning:
            TelegramClient(config).send_message(message)
        else:
            logging.info("health ok, nessun messaggio")
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
