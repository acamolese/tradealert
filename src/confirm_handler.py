"""Handler per i callback dei bottoni Telegram (Esegui / Salta / Budget).

Invocato dal listener daemon quando arriva un ``callback_query``. Mantiene
in memoria di processo il budget selezionato per ogni signal pending
(``DaemonState.staged_budgets``): se il daemon si riavvia, la scelta torna
al default del ``.env``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .capital_client import CapitalClient
from .config import Config
from .db import Database
from .executor import ExecutionResult, execute_signal
from .telegram_client import TelegramClient
from .universe import UNIVERSE

log = logging.getLogger(__name__)

VALID_CALLBACK_PREFIXES = ("exec:", "skip:", "budget:")


@dataclass
class DaemonState:
    staged_budgets: dict[int, float] = field(default_factory=dict)


def _parse_data(data: str) -> tuple[str, dict[str, Any]] | None:
    parts = data.split(":")
    action = parts[0]
    try:
        if action == "budget":
            return action, {
                "amount": float(parts[1]),
                "signal_id": int(parts[2]),
            }
        if action == "exec":
            # Formato: exec:<signal_id>:<budget_inline>
            return action, {
                "signal_id": int(parts[1]),
                "inline_budget": float(parts[2]) if len(parts) > 2 else None,
            }
        if action == "skip":
            return action, {"signal_id": int(parts[1])}
    except (IndexError, ValueError):
        return None
    return None


def _find_epic(asset_name: str) -> str | None:
    for a in UNIVERSE:
        if a.name == asset_name:
            return a.epic
    return None


def _format_execution_message(
    result: ExecutionResult, signal_row: dict[str, Any]
) -> str:
    if result.executed:
        return (
            f"✅ <b>Posizione aperta su Capital.com</b>\n\n"
            f"<b>{signal_row['asset']}</b> "
            f"{signal_row['direction'].upper()}\n"
            f"Size: <code>{result.size}</code>\n"
            f"Entry: <code>{result.entry_price}</code>\n"
            f"SL: <code>{result.stop_level}</code>\n"
            f"TP: <code>{result.profit_level}</code>\n"
            f"Deal ID: <code>{result.deal_id}</code>"
        )
    return (
        f"⚠️ <b>Esecuzione saltata</b>\n\n"
        f"Signal {signal_row['id']} ({signal_row['asset']}) non eseguito.\n"
        f"Motivo: <i>{result.reason}</i>"
    )


def handle_callback(
    data: str,
    cb: dict[str, Any],
    config: Config,
    db: Database,
    telegram: TelegramClient,
    state: DaemonState,
) -> None:
    parsed = _parse_data(data)
    if not parsed:
        log.warning("Callback data non riconosciuto: %s", data)
        return
    action, args = parsed
    signal_id = args["signal_id"]
    message_id = (cb.get("message") or {}).get("message_id")

    signal_row = db.get_signal(signal_id)
    if not signal_row:
        if message_id:
            telegram.edit_message_text(
                message_id, f"⚠️ <b>Signal {signal_id}</b> non trovato nel DB"
            )
        return
    status = signal_row.get("status")

    if action == "skip":
        if status == "pending":
            db.update_signal_status(signal_id, "skipped")
        if message_id:
            telegram.edit_message_text(
                message_id, f"❌ <b>Signal {signal_id} saltato</b>"
            )
        state.staged_budgets.pop(signal_id, None)
        return

    if action == "budget":
        if status != "pending":
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"⚠️ <b>Signal {signal_id}</b> non più modificabile "
                    f"(status={status})",
                )
            return
        new_budget = args["amount"]
        state.staged_budgets[signal_id] = new_budget
        log.info("Signal %s: budget staged a %.2f EUR", signal_id, new_budget)
        if message_id:
            # Import locale per evitare ciclo di import a livello modulo.
            from .scanner import _confirm_buttons

            try:
                telegram.edit_message_reply_markup(
                    message_id, _confirm_buttons(signal_id, new_budget)
                )
            except Exception:
                log.exception(
                    "Ri-edit bottoni fallito per signal %s", signal_id
                )
        return

    if action == "exec":
        if status != "pending":
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"⚠️ <b>Signal {signal_id}</b> già gestito "
                    f"(status={status})",
                )
            return
        effective_budget = state.staged_budgets.get(
            signal_id, args.get("inline_budget") or config.margin_budget_eur
        )
        if message_id:
            telegram.edit_message_text(
                message_id,
                f"⏳ <b>Apertura posizione in corso</b> "
                f"(signal {signal_id}, budget €{effective_budget:.0f})...",
            )

        epic = _find_epic(signal_row["asset"])
        if not epic:
            msg = (
                f"⚠️ Signal {signal_id}: epic per "
                f"{signal_row['asset']} non trovato in UNIVERSE"
            )
            telegram.send_message(msg)
            db.update_signal_status(signal_id, "error")
            return

        capital = CapitalClient(config)
        try:
            capital.login()
        except Exception as exc:
            telegram.send_message(
                f"⚠️ Signal {signal_id}: login Capital fallito: {exc}"
            )
            return

        asset_features = {
            "epic": epic,
            "last_price": signal_row.get("entry_price"),
        }
        result = execute_signal(
            config,
            capital,
            db,
            signal_row,
            asset_features,
            margin_budget_override=effective_budget,
        )
        telegram.send_message(_format_execution_message(result, signal_row))
        state.staged_budgets.pop(signal_id, None)
