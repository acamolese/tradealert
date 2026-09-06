"""Esercizio a capitale dichiarato: avvio e resoconto giornaliero.

Il conto di prova ha circa 980 €, l'esercizio si legge come se ne avesse 200
(richiesta 2026-09-06). Qui ci sono le due operazioni: far partire l'esercizio
da zero e mandare la sera il resoconto della giornata.

Uso:
  python -m jobs.grid_esercizio --avvia               # chiude tutto e riparte da 200 €
  python -m jobs.grid_esercizio --avvia --capitale 500
  python -m jobs.grid_esercizio --avvia --senza-chiudere
  python -m jobs.grid_esercizio --report              # resoconto della sera (cron)
  aggiungere --print per vedere il testo senza inviarlo su Telegram
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime

from src.config import load_config
from src.grid_esercizio import (CAPITALE_DEFAULT, avvia, leggi, messaggio,
                                registra_giorno, valore)
from src.grid_report import ROMA, raccogli

log = logging.getLogger(__name__)


def _arg(nome: str, default):
    if nome in sys.argv:
        try:
            return type(default)(sys.argv[sys.argv.index(nome) + 1])
        except (IndexError, ValueError):
            pass
    return default


def _client(env: str):
    from src.capital_client import CapitalClient
    os.environ["CAPITAL_ENV"] = env
    cfg = load_config()
    cap = CapitalClient(cfg)
    cap.login()
    return cfg, cap


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = "live" if "--reale" in sys.argv else "demo"
    solo_stampa = "--print" in sys.argv
    cfg, cap = _client(env)

    telegram = None
    if not solo_stampa:
        from src.telegram_client import TelegramClient
        telegram = TelegramClient(cfg)

    if "--avvia" in sys.argv:
        capitale = _arg("--capitale", CAPITALE_DEFAULT)
        testo = avvia(env, capitale, cap, telegram,
                      chiudi="--senza-chiudere" not in sys.argv)
        if solo_stampa:
            print(re.sub(r"<[^>]+>", "", testo))
        return 0

    st = leggi(env)
    if not st.get("capitale"):
        log.error("nessun esercizio in corso su %s: prima --avvia", env)
        return 1

    c = raccogli(cap, env, n_ultimi=0, con_valore=True)
    testo = messaggio(c, st)
    if c.ok and not solo_stampa:
        oggi = datetime.now(ROMA).date().isoformat()
        registra_giorno(env, st, {
            "data": oggi, "valore": round(valore(st, c.equity), 2),
            "delta": round(c.guadagno_oggi, 2),
            "movimenti": c.movimenti_oggi,
            "realizzato": round(c.realizzato_oggi, 2),
            "costi": round(c.costi_oggi, 2)})

    if solo_stampa:
        print(re.sub(r"<[^>]+>", "", testo))
        return 0
    telegram.send_message(testo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
