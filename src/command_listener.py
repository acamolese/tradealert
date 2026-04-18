"""Listener per comandi Telegram.

Due modalita':
- ``listen_forever``: daemon infinito (systemd), offset persistente nel processo.
- ``listen_once``: ascolta per N secondi, processa il primo comando e ritorna,
  confermando l'offset a Telegram cosi' i run successivi non lo ri-elaborano.
  Usato storicamente da GitHub Actions.
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


def _process_update(update: dict, config: Config, expected_chat: str) -> str | None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    if str(msg.get("chat", {}).get("id")) != expected_chat:
        return None
    text = (msg.get("text") or "").strip().lower()
    if text in KNOWN_COMMANDS:
        log.info("Comando %s ricevuto, lancio gestione posizioni", text)
        manage_positions(config)
        return text
    return None


def _commit_offset(base_url: str, offset: int) -> None:
    # Chiamata getUpdates con offset=next e timeout=0: rimuove dalla coda
    # Telegram tutti gli update con id < offset. Safe ignore in caso di errore.
    try:
        requests.get(base_url, params={"offset": offset, "timeout": 0}, timeout=5)
    except requests.RequestException:
        pass


def listen_once(config: Config, max_seconds: int = 60) -> str | None:
    """Polla per max_seconds. Ritorna il primo comando processato o None.
    Committa l'offset a Telegram prima di uscire."""
    telegram = TelegramClient(config)
    expected_chat = str(config.telegram_chat_id)
    base = f"{telegram._base}/getUpdates"
    deadline = time.time() + max_seconds
    offset = 0

    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        long_poll = min(25, remaining)
        try:
            r = requests.get(
                base, params={"offset": offset, "timeout": long_poll}, timeout=long_poll + 5
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("getUpdates errore: %s", exc)
            time.sleep(2)
            continue

        for update in r.json().get("result", []):
            offset = update["update_id"] + 1
            cmd = _process_update(update, config, expected_chat)
            if cmd:
                _commit_offset(base, offset)
                return cmd

    if offset:
        _commit_offset(base, offset)
    return None


def listen_forever(config: Config) -> None:
    """Daemon: loop infinito con long-polling. Mantiene l'offset nel processo,
    quindi nessuna ri-elaborazione. Restart=always di systemd resta solo come
    safety net in caso di crash."""
    telegram = TelegramClient(config)
    expected_chat = str(config.telegram_chat_id)
    base = f"{telegram._base}/getUpdates"
    offset = 0
    log.info("Listener daemon avviato (long-poll 25s)")

    while True:
        try:
            r = requests.get(
                base, params={"offset": offset, "timeout": 25}, timeout=30
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
        except requests.RequestException as exc:
            log.warning("getUpdates errore: %s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                _process_update(update, config, expected_chat)
            except Exception:
                log.exception("Errore processando update %s", update.get("update_id"))
