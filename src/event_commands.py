"""Comandi Telegram per gestire critical_events da chat:

- /evento <data> | <descrizione> | <asset1, asset2, ...> | <direction_hint>
  Aggiunge un evento manuale a config/critical_events.json.
- /eventi
  Lista gli eventi futuri attualmente in file (manuali e auto).
- /evento help
  Mostra la sintassi.

Lo scopo: non dover mai toccare il file JSON a mano sul server. Tu
aggiungi tutto da Telegram, il sistema persiste su disco e lo scanner
legge al prossimo tick.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .market_context import (
    append_manual_event,
    list_all_events,
    prune_past_events,
)
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)


_ALLOWED_HINTS = {
    "risk_on", "risk_off",
    "risk_off_if_fails", "risk_on_if_fails",
    "unknown",
}

_HELP_TEXT = (
    "<b>Uso /evento</b>\n"
    "<code>/evento data | descrizione | asset1, asset2 | direction</code>\n\n"
    "<b>data</b>: ISO-8601 UTC o formato <code>YYYY-MM-DD HH:MM</code> "
    "(interpretato come UTC)\n"
    "<b>descrizione</b>: testo libero (max 120 caratteri)\n"
    "<b>asset</b>: uno o piu', separati da virgola, dai nomi dell'universo "
    "(es. Gold, Brent Oil, US500)\n"
    "<b>direction</b>: uno di risk_on, risk_off, risk_off_if_fails, "
    "risk_on_if_fails, unknown\n\n"
    "<b>Esempio:</b>\n"
    "<code>/evento 2026-04-22 22:00 | Scadenza tregua Iran | "
    "Brent Oil, Gold, US500 | risk_off_if_fails</code>\n\n"
    "Altri comandi: <code>/eventi</code> per vedere la lista."
)


def _parse_user_date(raw: str) -> datetime | None:
    s = raw.strip().replace("Z", "+00:00")
    # Formati accettati: ISO completo; "YYYY-MM-DD HH:MM"; "YYYY-MM-DD".
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_event_line(ev: dict[str, Any]) -> str:
    date = ev.get("date", "?")
    desc = ev.get("description", "?")
    assets = ", ".join(ev.get("impact_assets") or [])
    hint = ev.get("direction_hint", "unknown")
    source = ev.get("source", "manual")
    tag = "🤖" if source == "auto" else "✍️"
    return (
        f"{tag} <code>{date}</code>\n"
        f"    <b>{desc}</b>\n"
        f"    <i>{assets}</i> — {hint}"
    )


def handle_eventi_list(config: Config) -> None:
    """Invia la lista degli eventi futuri su Telegram."""
    telegram = TelegramClient(config)
    prune_past_events()  # pulizia passiva a ogni richiesta
    events = list_all_events()
    # Ordina per data crescente
    def _k(e: dict[str, Any]) -> str:
        return str(e.get("date", ""))
    events.sort(key=_k)
    if not events:
        telegram.send_message(
            "📭 <b>Nessun evento critico registrato.</b>\n\n"
            "Aggiungili con <code>/evento</code> o attendi lo scan "
            "automatico delle news di domattina."
        )
        return
    lines = [_fmt_event_line(e) for e in events]
    msg = (
        "📌 <b>Eventi critici attivi</b>\n"
        "<i>🤖 = estratto dalle news, ✍️ = aggiunto da te</i>\n\n"
        + "\n\n".join(lines)
    )
    telegram.send_message(msg)


def handle_evento_add(config: Config, body: str) -> None:
    """Parsing del comando /evento <args>. Risponde su Telegram con
    esito ok/errore + help se la sintassi non torna."""
    telegram = TelegramClient(config)
    body = body.strip()

    if not body or body.lower() in ("help", "?", "aiuto"):
        telegram.send_message(_HELP_TEXT)
        return

    parts = [p.strip() for p in body.split("|")]
    if len(parts) != 4:
        telegram.send_message(
            "⚠️ Sintassi errata. Mi aspetto 4 campi separati da <code>|</code>.\n\n"
            + _HELP_TEXT
        )
        return

    date_raw, desc, assets_raw, hint = parts
    dt = _parse_user_date(date_raw)
    if dt is None:
        telegram.send_message(
            f"⚠️ Data non riconosciuta: <code>{date_raw}</code>\n"
            "Usa <code>YYYY-MM-DD HH:MM</code> (UTC) o ISO-8601."
        )
        return
    if dt < datetime.now(timezone.utc):
        telegram.send_message(
            f"⚠️ La data <code>{dt.isoformat()}</code> è nel passato, "
            "non la salvo."
        )
        return
    if not desc or len(desc) > 200:
        telegram.send_message(
            "⚠️ Descrizione mancante o troppo lunga (max 200 caratteri)."
        )
        return

    assets = [a.strip() for a in assets_raw.split(",") if a.strip()]
    if not assets:
        telegram.send_message(
            "⚠️ Devi indicare almeno un asset (es. <code>Brent Oil, Gold</code>)."
        )
        return

    hint_norm = hint.strip().lower()
    if hint_norm not in _ALLOWED_HINTS:
        telegram.send_message(
            f"⚠️ direction_hint non valido: <code>{hint}</code>\n"
            f"Ammessi: {', '.join(sorted(_ALLOWED_HINTS))}"
        )
        return

    event = {
        "date": dt.isoformat(),
        "description": desc,
        "impact_assets": assets,
        "direction_hint": hint_norm,
    }
    try:
        append_manual_event(event)
    except Exception as exc:
        log.exception("Salvataggio evento fallito")
        telegram.send_message(f"⚠️ Impossibile salvare l'evento: {exc}")
        return

    assets_fmt = ", ".join(assets)
    telegram.send_message(
        "✅ <b>Evento salvato.</b>\n\n"
        f"<code>{dt.isoformat()}</code>\n"
        f"<b>{desc}</b>\n"
        f"<i>{assets_fmt}</i> — {hint_norm}\n\n"
        "Verrà considerato dallo scanner nelle prossime 72h."
    )


_EVENTO_RE = re.compile(r"^/evento\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def try_handle_event_command(config: Config, text: str) -> str | None:
    """Se il messaggio e' un comando evento-related, lo gestisce e
    ritorna il comando riconosciuto. Altrimenti None."""
    t = text.strip()
    low = t.lower()
    if low in ("/eventi", "/events"):
        handle_eventi_list(config)
        return "/eventi"
    m = _EVENTO_RE.match(t)
    if m:
        handle_evento_add(config, m.group(1))
        return "/evento"
    return None
