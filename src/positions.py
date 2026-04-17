"""Gestione interattiva delle posizioni aperte su Capital.com.

Lista le posizioni aperte, le mostra su Telegram con bottoni di chiusura,
attende il click dell'utente e chiude la posizione richiesta. Aggiorna anche
la riga corrispondente in trades (close_price, pnl, exit_reason='manual').
"""

from __future__ import annotations

import logging
from typing import Any

from .capital_client import CapitalClient
from .config import Config
from .db import Database
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_pos(p: dict[str, Any]) -> str:
    pos = p.get("position", {}) or {}
    market = p.get("market", {}) or {}
    name = market.get("instrumentName") or pos.get("epic") or "?"
    direction = pos.get("direction", "?")
    size = pos.get("size", "?")
    entry = pos.get("level", "?")
    pnl = pos.get("upl") or pos.get("profitAndLoss") or pos.get("pnl")
    pnl_str = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "-"
    return (
        f"<b>{_esc(name)}</b> {direction}\n"
        f"Size: <code>{size}</code> | Entry: <code>{entry}</code> | "
        f"P&amp;L: <code>{pnl_str}</code>"
    )


def _format_positions_message(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "📭 <b>Nessuna posizione aperta</b>"
    blocks = "\n\n".join(_fmt_pos(p) for p in positions)
    return f"📊 <b>Posizioni aperte ({len(positions)})</b>\n\n{blocks}"


def _close_buttons(
    positions: list[dict[str, Any]],
) -> list[list[dict[str, str]]]:
    rows = []
    for p in positions:
        pos = p.get("position", {}) or {}
        market = p.get("market", {}) or {}
        deal_id = pos.get("dealId")
        if not deal_id:
            continue
        name = market.get("instrumentName") or pos.get("epic") or deal_id
        rows.append(
            [
                {
                    "text": f"🔴 Chiudi {name}",
                    "callback_data": f"close:{deal_id}",
                }
            ]
        )
    rows.append([{"text": "✖️ Annulla", "callback_data": "cancel"}])
    return rows


def manage_positions(config: Config) -> None:
    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)

    capital.login()
    positions = capital.get_open_positions()

    if not positions:
        telegram.send_message(_format_positions_message(positions))
        return

    start_offset = telegram.drain_updates()
    sent = telegram.send_message_with_buttons(
        _format_positions_message(positions),
        _close_buttons(positions),
    )
    message_id = sent.get("message_id")

    data, _, _ = telegram.wait_for_callback(
        valid_prefixes=("close:", "cancel"),
        timeout_sec=config.confirm_timeout_sec,
        start_offset=start_offset,
    )

    if data is None:
        if message_id:
            telegram.edit_message_text(
                message_id,
                "⌛ <b>Timeout</b>\nNessuna azione di chiusura ricevuta.",
            )
        return

    if data == "cancel":
        if message_id:
            telegram.edit_message_text(
                message_id, "✖️ <b>Operazione annullata</b>"
            )
        return

    deal_id = data.split(":", 1)[1]
    if message_id:
        telegram.edit_message_text(
            message_id, f"⏳ <b>Chiusura {deal_id} in corso...</b>"
        )

    try:
        close_resp = capital.close_position(deal_id)
        deal_ref = close_resp.get("dealReference")
        confirm = capital.confirm_deal(deal_ref) if deal_ref else {}
        close_level = float(confirm.get("level") or 0)
        pnl = confirm.get("profit") or confirm.get("profitAndLoss")
        pnl = float(pnl) if pnl is not None else None

        # Aggiorna DB se troviamo il trade
        trade = db.get_trade_by_deal_id(deal_id)
        if trade and close_level > 0:
            entry = float(trade.get("entry_price") or 0)
            pnl_pct = (
                ((close_level - entry) / entry * 100) if entry else None
            )
            if trade.get("direction") == "short" and pnl_pct is not None:
                pnl_pct = -pnl_pct
            db.close_trade(
                deal_id,
                close_price=close_level,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason="manual",
            )
            db.insert_monitoring_event(
                {
                    "trade_id": trade["id"],
                    "event_type": "manual_close",
                    "reason": "Chiusura richiesta da utente via /posizioni",
                    "details": {"close_level": close_level, "pnl": pnl},
                }
            )

        telegram.send_message(
            f"✅ <b>Posizione chiusa</b>\n"
            f"Deal: <code>{_esc(deal_id)}</code>\n"
            f"Prezzo: <code>{close_level}</code>\n"
            f"P&amp;L: <code>{pnl}</code>"
        )
    except Exception as exc:
        log.exception("Chiusura fallita")
        telegram.send_message(
            f"⚠️ <b>Chiusura fallita</b>\n<i>{_esc(exc)}</i>"
        )
