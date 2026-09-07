"""Listener Telegram daemon.

Gestisce l'intero polling degli update del bot:
- comandi dei grid (dal 2026-09-03): /stato, /posizioni, /oggi, /ferma,
  /riparti, /aiuto. I comandi che muovono denaro chiedono conferma con un
  bottone (callback ``g2:``). I vecchi /status e /posizioni della v1 (scanner
  spento) sono stati rimossi: descrivevano scan e segnali che non esistono piu'.
- ``callback_query`` con prefisso ``exec:`` / ``skip:`` / ``budget:`` ->
  ``handle_callback`` (gestione signal in attesa di conferma, v1 legacy)

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
from .event_commands import try_handle_event_command
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)

# Comandi che leggono e basta. /stat, /statN, /conti restano come alias.
GRID_COMMANDS = ("/stato", "/status", "/stat", "/conti", "/grid", "/posizioni",
                 "/positions", "/oggi", "/aiuto", "/help", "/start", "/esercizio", "/claudetrade",
                 "/pagina", "/link", "/cruscotto")
# Comandi che muovono denaro: /ferma <reale|prova>, /riparti <reale|prova>.
GRID_ACTIONS = ("/ferma", "/riparti")
KNOWN_COMMANDS = GRID_COMMANDS + GRID_ACTIONS


def _g2_soglie(env: str) -> tuple[float, float]:
    """Soglie di profitto del conto (stesse env del job grid2)."""
    from .grid_control import soglie_conto
    s = soglie_conto(env)
    return s["profit_alert"], s["profit_stop"]


def _messaggio_esercizio(env: str = "demo") -> str:
    """Il resoconto di ClaudeTrade, su richiesta."""
    import os

    from .grid_esercizio import leggi, messaggio
    from .grid_report import raccogli
    from jobs.grid_esercizio import _client

    st = leggi(env)
    if not st.get("capitale"):
        return ("ClaudeTrade non è ancora partito. "
                "Si avvia dalla VM con <code>--avvia</code>.")
    originale = os.environ.get("CAPITAL_ENV")
    try:
        _, cap = _client(env)
        return messaggio(raccogli(cap, env, n_ultimi=0, con_valore=True), st,
                         "ClaudeTrade, situazione adesso")
    finally:
        if originale is None:
            os.environ.pop("CAPITAL_ENV", None)
        else:
            os.environ["CAPITAL_ENV"] = originale


def _handle_grid_command(config: Config, text: str) -> bool:
    """Comandi dei grid. Ritorna True se il comando e' stato gestito."""
    import re

    from .grid_control import ENV_DA_PAROLA, nome_in_frase
    from .grid_report import (messaggio_aiuto, messaggio_oggi,
                              messaggio_posizioni, messaggio_stato)
    from jobs.grid_hourly import leggi_conti

    parts = text.split()
    cmd = parts[0]
    m_stat = re.fullmatch(r"/stat(\d+)", cmd)
    m_oggi = re.fullmatch(r"/oggi(\d*)", cmd)
    if not (cmd in KNOWN_COMMANDS or m_stat or m_oggi):
        return False

    telegram = TelegramClient(config)
    try:
        if cmd in ("/aiuto", "/help", "/start"):
            telegram.send_message(messaggio_aiuto())
        elif cmd in GRID_ACTIONS:
            env = ENV_DA_PAROLA.get(parts[1]) if len(parts) > 1 else None
            if not env:
                telegram.send_message(
                    f"Dimmi quale conto: <b>{cmd} reale</b> oppure <b>{cmd} prova</b>.")
                return True
            azione = cmd[1:]
            if azione == "ferma":
                testo = (f"Vuoi davvero <b>fermare {nome_in_frase(env)}</b>?\n"
                         f"Chiudo tutte le posizioni aperte e nessun grid riapre "
                         f"finché non scrivi /riparti.")
            else:
                testo = (f"Vuoi <b>far ripartire {nome_in_frase(env)}</b>?\n"
                         f"La cifra attuale diventa la nuova base da cui contare "
                         f"guadagni e perdite.")
            telegram.send_message_with_buttons(testo, [[
                {"text": "✅ Sì, procedi", "callback_data": f"g2:{azione}:{env}"},
                {"text": "✖️ No", "callback_data": "g2:annulla:-"}]])
        elif m_oggi or m_stat:
            n = int((m_oggi or m_stat).group(1) or 0)
            conti = leggi_conti(max(n, 10))
            telegram.send_message(messaggio_oggi(conti, n))
        elif cmd in ("/pagina", "/link", "/cruscotto"):
            from .grid_esercizio import messaggio_pagina
            telegram.send_message(messaggio_pagina())
        elif cmd in ("/esercizio", "/claudetrade"):
            telegram.send_message(_messaggio_esercizio())
        elif cmd in ("/posizioni", "/positions"):
            conti = leggi_conti(con_valore=True)
            telegram.send_message(messaggio_posizioni(conti))
        else:
            conti = leggi_conti()
            telegram.send_message(messaggio_stato(conti))
    except Exception as exc:
        log.exception("comando grid fallito")
        telegram.send_message(f"Non ci sono riuscito ({type(exc).__name__}). Riprova tra poco.")
    return True


def _handle_grid_callback(config: Config, telegram: TelegramClient, cb: dict) -> None:
    """Conferma dei bottoni /ferma e /riparti (callback ``g2:<azione>:<env>``)."""
    from .grid_control import ferma, riparti

    data = cb.get("data", "")
    message_id = (cb.get("message") or {}).get("message_id")
    try:
        _, azione, env = data.split(":")
    except ValueError:
        return
    if message_id:
        try:
            telegram.edit_message_reply_markup(message_id, [])
        except Exception:
            pass
    if azione == "annulla" or env not in ("live", "demo"):
        telegram.send_message("Ok, non ho fatto nulla.")
        return
    try:
        if azione == "ferma":
            ferma(env, telegram)
        elif azione == "riparti":
            riparti(env, telegram, *_g2_soglie(env))
    except Exception as exc:
        log.exception("azione grid %s fallita", data)
        telegram.send_message(f"Non ci sono riuscito ({type(exc).__name__}). "
                              f"Controlla sull'app del broker e riprova.")


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
    raw_text = (msg.get("text") or "").strip()
    text = raw_text.lower()
    if _handle_grid_command(config, text):
        log.info("Comando grid %s gestito", text)
        return text
    # Comandi gestione eventi critici (case-insensitive, con argomenti)
    handled = try_handle_event_command(config, raw_text)
    if handled:
        log.info("Comando %s ricevuto, gestione evento", handled)
        return handled
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
    if data.startswith("g2:"):
        _answer_callback(telegram, cb["id"], "Ricevuto")
        _handle_grid_callback(config, telegram, cb)
        return True
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
