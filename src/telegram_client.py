"""Client minimale per il bot Telegram.

Supporta:
- send_message: invio one-way
- send_message_with_buttons: invio con inline keyboard (callback)
- wait_for_callback: polling getUpdates per attendere il click su un bottone
- edit_message: aggiorna messaggio (es. per togliere i bottoni dopo click)

Telegram in parse_mode=HTML accetta solo: b, strong, i, em, u, ins, s, strike,
del, code, pre, a, tg-spoiler, blockquote. Tag come <br>, <p>, <div> rompono
il parsing. send_message normalizza i piu' comuni per evitare 400 inutili.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from .config import Config

log = logging.getLogger(__name__)


_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "a", "tg-spoiler", "blockquote",
}
_BREAK_TAG_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_PARA_OPEN_RE = re.compile(r"<\s*p\s*>", re.IGNORECASE)
_PARA_CLOSE_RE = re.compile(r"<\s*/\s*p\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def sanitize_telegram_html(text: str) -> str:
    """Rimuove o converte tag HTML non supportati da Telegram."""
    text = _BREAK_TAG_RE.sub("\n", text)
    text = _PARA_OPEN_RE.sub("", text)
    text = _PARA_CLOSE_RE.sub("\n\n", text)

    def _strip(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        if tag in _ALLOWED_TAGS:
            return match.group(0)
        return ""

    text = _TAG_RE.sub(_strip, text)
    # Collassa run lunghissime di newline
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class TelegramClient:
    def __init__(self, config: Config) -> None:
        self._token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id  # owner (bottoni, edit, comandi)
        self._chat_ids = list(config.telegram_chat_ids or [self._chat_id])
        self._base = f"https://api.telegram.org/bot{self._token}"

    # ---------- send ----------

    def send_message(
        self, text: str, parse_mode: str = "HTML"
    ) -> dict[str, Any]:
        """Broadcast su tutti i chat configurati in TELEGRAM_CHAT_IDS.
        Ritorna la response del chat owner (il primo) per retrocompat."""
        if parse_mode == "HTML":
            text = sanitize_telegram_html(text)
        first_result: dict[str, Any] = {}
        for chat_id in self._chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                result = self._post("/sendMessage", payload)
                if not first_result:
                    first_result = result
            except Exception:
                log.exception(
                    "send_message fallito su chat_id %s (continuo con gli altri)",
                    chat_id,
                )
        return first_result

    def send_message_with_buttons(
        self,
        text: str,
        buttons: list[list[dict[str, str]]],
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """buttons e' una matrice di {text, callback_data}."""
        if parse_mode == "HTML":
            text = sanitize_telegram_html(text)
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
        if parse_mode == "HTML":
            text = sanitize_telegram_html(text)
        payload = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        return self._post("/editMessageText", payload)

    def edit_message_reply_markup(
        self, message_id: int, buttons: list[list[dict[str, str]]]
    ) -> dict[str, Any]:
        payload = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": buttons},
        }
        return self._post("/editMessageReplyMarkup", payload)

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
