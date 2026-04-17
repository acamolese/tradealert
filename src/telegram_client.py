"""Client minimale per il bot Telegram.

Supporta:
- send_message: invio one-way
- send_message_with_buttons: invio con inline keyboard (callback)
- wait_for_callback: polling getUpdates per attendere il click su un bottone
- edit_message: aggiorna messaggio (es. per togliere i bottoni dopo click)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import Config

log = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, config: Config) -> None:
        self._token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id
        self._base = f"https://api.telegram.org/bot{self._token}"

    # ---------- send ----------

    def send_message(self, text: str, parse_mode: str = "HTML") -> dict[str, Any]:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        return self._post("/sendMessage", payload)

    def send_message_with_buttons(
        self,
        text: str,
        buttons: list[list[dict[str, str]]],
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """buttons e' una matrice di {text, callback_data}."""
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons},
        }
        return self._post("/sendMessage", payload)

    def edit_message_text(
        self, message_id: int, text: str, parse_mode: str = "HTML"
    ) -> dict[str, Any]:
        payload = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        return self._post("/editMessageText", payload)

    # ---------- callbacks ----------

    def drain_updates(self) -> int:
        """Consuma gli update pendenti e ritorna il prossimo offset.

        Da chiamare PRIMA di mandare un messaggio interattivo, per evitare
        di processare callback arretrati di altri signal.
        """
        r = requests.get(
            f"{self._base}/getUpdates", params={"timeout": 0}, timeout=10
        )
        r.raise_for_status()
        results = r.json().get("result", [])
        if not results:
            return 0
        return results[-1]["update_id"] + 1

    def wait_for_callback(
        self,
        valid_prefixes: tuple[str, ...],
        timeout_sec: int,
        start_offset: int,
    ) -> tuple[str | None, dict[str, Any] | None, int]:
        """Polla getUpdates aspettando un callback con callback_data che
        inizia con uno dei valid_prefixes.

        Ritorna (callback_data, callback_query, next_offset). Se nessun
        callback arriva entro timeout, ritorna (None, None, next_offset).
        Il next_offset DEVE essere riusato dal chiamante per evitare di
        rileggere lo stesso update piu' volte.
        """
        deadline = time.time() + timeout_sec
        offset = start_offset
        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            long_poll = min(25, remaining)
            try:
                r = requests.get(
                    f"{self._base}/getUpdates",
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
                cb = update.get("callback_query")
                if not cb:
                    continue
                data = cb.get("data", "")
                if any(data.startswith(p) for p in valid_prefixes):
                    self._answer_callback(cb["id"], "Ricevuto")
                    return data, cb, offset
                # Callback non pertinente: lo "ack" comunque per togliere
                # il loading sul client di chi ha cliccato.
                self._answer_callback(cb["id"], "Ignorato")
        return None, None, offset

    def _answer_callback(self, callback_id: str, text: str) -> None:
        try:
            requests.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except requests.RequestException:
            pass

    # ---------- internals ----------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(f"{self._base}{path}", json=payload, timeout=10)
        if not r.ok:
            log.error("Telegram %s on %s: %s", r.status_code, path, r.text)
            r.raise_for_status()
        return r.json().get("result", {})
