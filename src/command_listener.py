"""Listener per comandi Telegram.

Polla getUpdates per N secondi cercando messaggi-comando dell'utente.
Se trova /posizioni, lancia il flusso di gestione posizioni interattivo.

E' pensato per essere richiamato sia a mano sia da un cron GitHub Actions:
ogni esecuzione drena gli update arretrati e processa il primo comando
riconosciuto, poi esce.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import Config
from .positions import manage_positions
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)

KNOWN_COMMANDS = ("/posizioni", "/positions")


def listen_once(config: Config, max_seconds: int = 60) -> str | None:
    """Polla per max_seconds. Ritorna il comando processato o None."""
    telegram = TelegramClient(config)
    expected_chat = str(config.telegram_chat_id)
    deadline = time.time() + max_seconds
    offset = 0

    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        long_poll = min(25, remaining)
        try:
            r = requests.get(
                f"{telegram._base}/getUpdates",
                params={"offset": offset, "timeout": long_poll},
                timeout=long_poll + 5,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("getUpdates errore: %s", exc)
            time.sleep(2)
            continue

        for update in r.json().get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            if str(chat_id) != expected_chat:
                # Ignora messaggi di altri utenti
                continue
            text = (msg.get("text") or "").strip().lower()
            if not text:
                continue
            if text in KNOWN_COMMANDS:
                log.info("Comando %s ricevuto, lancio gestione posizioni", text)
                manage_positions(config)
                return text
            # Altri messaggi: ignorati (no help spam)
    return None
