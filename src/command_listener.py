"""Listener Telegram daemon.

Gestisce l'intero polling degli update del bot:
- ``message`` con testo ``/posizioni`` o ``/positions`` -> ``manage_positions``
- ``callback_query`` con prefisso ``exec:`` / ``skip:`` / ``budget:`` ->
  ``handle_callback`` (gestione signal in attesa di conferma)

``listen_once`` e' mantenuta per uso CLI / cron legacy. Confirma l'offset
a Telegram prima di uscire, cosi' i run successivi non ri-processano.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import Config
from .confirm_handler import (
    VALID_CALLBACK_PREFIXES,
    DaemonState,
    handle_callback,
)
from .db import Database
from .positions import manage_positions
from .status import send_status
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)

POSITIONS_COMMANDS = ("/posizioni", "/positions")
STATUS_COMMANDS = ("/status", "/stato")
KNOWN_COMMANDS = POSITIONS_COMMANDS + STATUS_COMMANDS


def _answer_callback(telegram: TelegramClient, cb_id: str, text: str = "Ricevuto") -> None:
    try:
        requests.post(
            f"{telegram._base}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": text},
            timeout=5,
        )
    except requests.RequestException:
        pass


def _process_message(
    update: dict, config: Config, expected_chat: str
) -> str | None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    if str(msg.get("chat", {}).get("id")) != expected_chat:
        return None
    text = (msg.get("text") or "").strip().lower()
    if text in POSITIONS_COMMANDS:
        log.info("Comando %s ricevuto, lancio gestione posizioni", text)
        manage_positions(config)
        return text
    if text in STATUS_COMMANDS:
        log.info("Comando %s ricevuto, invio status", text)
        send_status(config)
        return text
    return None


def _process_callback(
    update: dict,
    config: Config,
    db: Database,
    telegram: TelegramClient,
    state: DaemonState,
    expected_chat: str,
) -> bool:
    cb = update.get("callback_query")
    if not cb:
        return False
    if str((cb.get("message") or {}).get("chat", {}).get("id")) != expected_chat:
        _answer_callback(telegram, cb["id"], "Non autorizzato")
        return True
    data = cb.get("data", "")
    if not any(data.startswith(p) for p in VALID_CALLBACK_PREFIXES):
        _answer_callback(telegram, cb["id"], "Ignorato")
        return True
    _answer_callback(telegram, cb["id"], "Ricevuto")
    handle_callback(data, cb, config, db, telegram, state)
    return True


def _commit_offset(base_url: str, offset: int) -> None:
    try:
        requests.get(base_url, params={"offset": offset, "timeout": 0}, timeout=5)
    except requests.RequestException:
        pass


def listen_once(config: Config, max_seconds: int = 60) -> str | None:
    """Polla per ``max_seconds``. Ritorna il primo comando ``/posizioni``
    processato o None. Committa l'offset prima di uscire."""
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
                base,
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
            cmd = _process_message(update, config, expected_chat)
            if cmd:
                _commit_offset(base, offset)
                return cmd

    if offset:
        _commit_offset(base, offset)
    return None


def listen_forever(config: Config) -> None:
    """Daemon: loop infinito con long-polling. Gestisce messaggi-comando
    e callback dei bottoni confirm. Offset persistente in memoria."""
    telegram = TelegramClient(config)
    db = Database(config)
    state = DaemonState()
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
                if _process_callback(
                    update, config, db, telegram, state, expected_chat
                ):
                    continue
                _process_message(update, config, expected_chat)
            except Exception:
                log.exception(
                    "Errore processando update %s", update.get("update_id")
                )
