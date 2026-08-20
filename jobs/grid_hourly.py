"""Riepilogo orario su Telegram dei due conti (reale + demo).

Legge entrambi i conti nella stessa esecuzione: e' l'unico modo per confrontare
la stessa strategia a taglie diverse. In cron ogni ora.

Uso:
  python -m jobs.grid_hourly              # invia il riepilogo
  python -m jobs.grid_hourly --print      # stampa e basta
  python -m jobs.grid_hourly --ultimi 10  # invia gli ultimi N movimenti
"""
from __future__ import annotations

import logging
import os
import sys

from src.config import load_config
from src.grid_report import raccogli, messaggio_riepilogo, messaggio_ultimi

log = logging.getLogger(__name__)


def leggi_conti(n_ultimi: int = 10):
    """Fotografa reale e demo. Il client Capital sceglie l'ambiente da
    CAPITAL_ENV, quindi si istanzia due volte con la variabile diversa."""
    from src.capital_client import CapitalClient

    conti = []
    originale = os.environ.get("CAPITAL_ENV")
    for env, nome in (("live", "REALE"), ("demo", "DEMO")):
        try:
            os.environ["CAPITAL_ENV"] = env
            cfg = load_config()
            cap = CapitalClient(cfg)
            cap.login()
            conti.append(raccogli(cap, env, nome, n_ultimi))
        except Exception:
            log.exception("lettura conto %s fallita", env)
    if originale is None:
        os.environ.pop("CAPITAL_ENV", None)
    else:
        os.environ["CAPITAL_ENV"] = originale
    return conti


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    solo_stampa = "--print" in sys.argv
    n = 0
    if "--ultimi" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--ultimi") + 1])
        except (IndexError, ValueError):
            n = 10

    conti = leggi_conti(max(n, 10))
    if not conti:
        log.error("nessun conto leggibile")
        return 1
    testo = messaggio_ultimi(conti, n) if n else messaggio_riepilogo(conti)

    if solo_stampa:
        import re
        print(re.sub(r"<[^>]+>", "", testo))
        return 0

    from src.telegram_client import TelegramClient
    TelegramClient(load_config()).send_message(testo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
