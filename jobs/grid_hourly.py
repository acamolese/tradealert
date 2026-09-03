"""Messaggi automatici sui due conti (reale + di prova) e lettura per i comandi.

Legge entrambi i conti nella stessa esecuzione: e' l'unico modo per confrontare
la stessa strategia a taglie diverse.

Dal 2026-09-03 non c'e' piu' il riepilogo ogni ora (24 messaggi al giorno,
anche di notte e nel weekend a mercati chiusi): arrivano un buongiorno alle 8 e
una chiusura alle 22:30 nei giorni di mercato, il resto si chiede con /stato.

Uso:
  python -m jobs.grid_hourly --mattina        # buongiorno
  python -m jobs.grid_hourly --sera           # chiusura di giornata
  python -m jobs.grid_hourly                  # situazione adesso (come /stato)
  python -m jobs.grid_hourly --posizioni
  python -m jobs.grid_hourly --oggi [--ultimi N]
  aggiungere --print per stampare senza inviare
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from src.config import load_config
from src.grid_report import (messaggio_mattina, messaggio_oggi,
                             messaggio_posizioni, messaggio_sera,
                             messaggio_stato, raccogli)

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"


def leggi_conti(n_ultimi: int = 10, con_valore: bool = False):
    """Fotografa reale e demo. Il client Capital sceglie l'ambiente da
    CAPITAL_ENV, quindi si istanzia due volte con la variabile diversa."""
    from src.capital_client import CapitalClient
    from src.grid_report import Conto

    conti = []
    originale = os.environ.get("CAPITAL_ENV")
    for env in ("live", "demo"):
        try:
            os.environ["CAPITAL_ENV"] = env
            cfg = load_config()
            cap = CapitalClient(cfg)
            cap.login()
            conti.append(raccogli(cap, env, n_ultimi=n_ultimi, con_valore=con_valore))
        except Exception:
            log.exception("lettura conto %s fallita", env)
            from src.grid_control import nome_conto
            conti.append(Conto(nome=nome_conto(env), env=env, ok=False))
    if originale is None:
        os.environ.pop("CAPITAL_ENV", None)
    else:
        os.environ["CAPITAL_ENV"] = originale
    return conti


def sistema_ok() -> bool | None:
    """Esito dell'ultimo health check (scritto da jobs/health_check.py)."""
    try:
        d = json.loads((DATA / "health_last.json").read_text())
        return bool(d.get("ok"))
    except Exception:
        return None


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

    conti = leggi_conti(max(n, 10), con_valore="--posizioni" in sys.argv)
    if "--mattina" in sys.argv:
        testo = messaggio_mattina(conti, sistema_ok())
    elif "--sera" in sys.argv:
        testo = messaggio_sera(conti)
    elif "--posizioni" in sys.argv:
        testo = messaggio_posizioni(conti)
    elif "--oggi" in sys.argv or n:
        testo = messaggio_oggi(conti, n)
    else:
        testo = messaggio_stato(conti)

    if solo_stampa:
        import re
        print(re.sub(r"<[^>]+>", "", testo))
        return 0

    from src.telegram_client import TelegramClient
    TelegramClient(load_config()).send_message(testo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
