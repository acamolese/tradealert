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
from .executor import ExecutionResult, _market_meta, execute_signal
from .position_monitor import close_position_by_deal_id
from .risk import calculate_size
from .telegram_client import TelegramClient
from .universe import UNIVERSE

log = logging.getLogger(__name__)

VALID_CALLBACK_PREFIXES = (
    "exec:",
    "skip:",
    "budget:",
    "mclose:",
    "mhold:",
    "rot:",
)


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
            return action, {
                "signal_id": int(parts[1]),
                "inline_budget": float(parts[2]) if len(parts) > 2 else None,
            }
        if action == "skip":
            return action, {"signal_id": int(parts[1])}
        if action in ("mclose", "mhold"):
            return action, {"deal_id": parts[1]}
        if action == "rot":
            # rot:exec:<signal_id>:<old_deal_id>
            # rot:open:<signal_id>
            # rot:skip:<signal_id>
            sub = parts[1]
            out: dict[str, Any] = {"sub": sub, "signal_id": int(parts[2])}
            if sub == "exec" and len(parts) > 3:
                out["old_deal_id"] = parts[3]
            return action, out
    except (IndexError, ValueError):
        return None
    return None


def _handle_monitor_callback(
    action: str,
    args: dict[str, Any],
    cb: dict[str, Any],
    config: Config,
    db: Database,
    telegram: TelegramClient,
) -> None:
    deal_id = args["deal_id"]
    message_id = (cb.get("message") or {}).get("message_id")

    if action == "mhold":
        # Registra il veto: l'auto-close (se attivo) poll-a questo evento per
        # annullare la chiusura entro la finestra. Harmless se auto-close OFF.
        try:
            trade = db.get_trade_by_deal_id(deal_id)
            if trade and trade.get("id"):
                db.insert_monitoring_event({
                    "trade_id": trade["id"],
                    "event_type": "close_vetoed",
                    "reason": "utente: lascia aperta (veto auto-close)",
                    "details": {"deal_id": deal_id},
                })
        except Exception:
            pass
        if message_id:
            telegram.edit_message_text(
                message_id, "⏸ <b>Posizione lasciata aperta</b>"
            )
        return

    # mclose
    if message_id:
        telegram.edit_message_text(
            message_id, "⏳ <b>Chiusura in corso...</b>"
        )
    capital = CapitalClient(config)
    try:
        capital.login()
    except Exception as exc:
        telegram.send_message(f"⚠️ Login Capital fallito: {exc}")
        return
    close_position_by_deal_id(
        capital, db, telegram, deal_id,
        reason="Richiesta utente da monitor",
        message_id=message_id,
    )


def _resolve_epic(
    signal_row: dict[str, Any], capital: CapitalClient | None = None
) -> str | None:
    """Risolve l'epic in questo ordine:
    1. ``signal_row['epic']`` se presente (signal nuovi dopo migration)
    2. UNIVERSE statico per nome (signal vecchi)
    3. Capital ``search_market`` come ultima risorsa (signal discovery
       dinamica pre-migration: cerca per nome e prende il primo match
       con instrumentType coerente).
    """
    epic = signal_row.get("epic")
    if epic:
        return epic
    name = signal_row.get("asset") or ""
    for a in UNIVERSE:
        if a.name == name:
            return a.epic
    if capital is None:
        return None
    try:
        # Estrai il simbolo "principale" dal nome: "GTC/USD" -> "GTC"
        query = name.split("/")[0].strip() or name
        for market in capital.search_market(query):
            if market.get("instrumentName") == name:
                return market.get("epic")
        # Nessun match esatto sul nome: abbandono, piu' sicuro che
        # aprire posizione sull'epic sbagliato.
    except Exception:
        log.exception("search_market fallita per %s", name)
    return None


def _build_confirm_text(
    signal_row: dict[str, Any],
    margin_budget: float,
    sizing,
) -> str:
    """Versione leggera di _format_confirm_message per il ri-edit on
    budget click: non ha key_factors/risks/news (non persistiti)."""
    from .scanner import _direction_label
    asset = signal_row.get("asset", "?")
    direction_line = _direction_label(signal_row.get("direction") or "")
    score = signal_row.get("score", "?")
    thesis = signal_row.get("thesis", "") or ""

    if sizing is None or sizing.size is None:
        reason = getattr(sizing, "reason", "")
        sizing_block = (
            f"⚠️ Con budget {margin_budget:.0f} EUR il sizing non passa: {reason}"
        )
    else:
        sizing_block = (
            f"<b>Preview con budget {margin_budget:.0f} EUR "
            f"(= margine sul conto):</b>\n"
            f"Margine bloccato: <code>{sizing.margin_estimate:.2f} EUR</code>\n"
            f"Size: <code>{sizing.size:g}</code>\n"
            f"Esposizione generata: <code>{sizing.notional:.2f} EUR</code>\n"
            f"Rischio se SL: <code>{sizing.risk_estimate:.2f} EUR</code>"
        )

    return (
        f"🟡 <b>Conferma richiesta</b> (signal {signal_row.get('id')})\n\n"
        f"<b>{asset}</b>  (score {score}/10)\n"
        f"{direction_line}\n\n"
        f"<i>Thesis:</i>\n{thesis}\n\n"
        f"{sizing_block}\n\n"
        f"<i>I bottoni sotto sono l'importo in EUR da bloccare come margine.</i>"
    )


def _check_entry_still_valid(
    signal_row: dict[str, Any],
    current_price: float,
    min_rr: float,
) -> tuple[bool, str]:
    """Verifica che il R:R rispetto ai livelli originali del setup sia
    ancora >= min_rr al prezzo corrente. Tra messaggio e click puo'
    passare tempo: se l'edge e' gia' stato consumato, meglio rinunciare
    invece di entrare in ritardo. Ritorna (ok, motivo_se_skip)."""
    direction = (signal_row.get("direction") or "").upper()
    entry_orig = float(signal_row.get("entry_price") or 0)
    sl_pct = float(signal_row.get("stop_loss") or 0)
    tp_pct = float(signal_row.get("take_profit") or 0)
    if entry_orig <= 0 or sl_pct <= 0 or tp_pct <= 0:
        return True, ""  # dati mancanti: non blocco l'apertura

    if direction == "LONG":
        sl_orig = entry_orig * (1 - sl_pct / 100)
        tp_orig = entry_orig * (1 + tp_pct / 100)
        risk_live = current_price - sl_orig
        reward_live = tp_orig - current_price
    elif direction == "SHORT":
        sl_orig = entry_orig * (1 + sl_pct / 100)
        tp_orig = entry_orig * (1 - tp_pct / 100)
        risk_live = sl_orig - current_price
        reward_live = current_price - tp_orig
    else:
        return True, ""

    if risk_live <= 0:
        return False, (
            f"prezzo {current_price:g} oltre lo SL originale "
            f"{sl_orig:g}: setup invalidato"
        )
    if reward_live <= 0:
        return False, (
            f"prezzo {current_price:g} ha già raggiunto il TP originale "
            f"{tp_orig:g}: edge consumato"
        )
    rr_live = reward_live / risk_live
    if rr_live < min_rr:
        return False, (
            f"R:R degradato a {rr_live:.2f} (soglia {min_rr:.2f}), "
            f"entry mossa da {entry_orig:g} a {current_price:g}"
        )
    return True, ""


def _fetch_current_mid(
    capital: CapitalClient, epic: str
) -> float | None:
    """Mid price corrente per epic, None se fetch fallisce."""
    try:
        market = capital.get_market(epic)
    except Exception:
        log.exception("Fetch market %s fallito", epic)
        return None
    return _market_meta(market).get("mid_price")


def _recompute_sizing_for_signal(
    config: Config,
    signal_row: dict[str, Any],
    margin_budget: float,
):
    """Fetch market Capital + ricalcola sizing. Ritorna SizingResult o None."""
    capital = CapitalClient(config)
    try:
        capital.login()
    except Exception:
        log.exception("Recompute sizing: login fallito")
        return None
    epic = _resolve_epic(signal_row, capital)
    if not epic:
        return None
    try:
        market = capital.get_market(epic)
    except Exception:
        log.exception("Recompute sizing: fetch market fallito")
        return None
    leverages_map = capital.get_leverages_map()
    meta = _market_meta(market, leverages_map=leverages_map)
    entry = meta["mid_price"] or signal_row.get("entry_price") or 0
    stop_pct = float(signal_row.get("stop_loss") or 1.5)
    return calculate_size(
        margin_budget=margin_budget,
        entry_price=float(entry),
        margin_factor=meta["margin_factor"],
        min_size=meta["min_size"],
        size_step=meta["size_step"],
        stop_pct=stop_pct,
        max_loss_per_trade_eur=config.max_loss_per_trade_eur,
    )


def _handle_rotation_callback(
    args: dict[str, Any],
    cb: dict[str, Any],
    config: Config,
    db: Database,
    telegram: TelegramClient,
    state: "DaemonState",
) -> None:
    sub = args.get("sub")
    signal_id = args["signal_id"]
    message_id = (cb.get("message") or {}).get("message_id")

    signal_row = db.get_signal(signal_id)
    if not signal_row or signal_row.get("status") != "pending":
        if message_id:
            telegram.edit_message_text(
                message_id,
                f"⚠️ <b>Signal {signal_id}</b> non più modificabile",
            )
        return

    if sub == "skip":
        db.update_signal_status(signal_id, "skipped")
        if message_id:
            telegram.edit_message_text(
                message_id,
                f"❌ <b>Rotation ignorata</b> (signal {signal_id})",
            )
        return

    capital = CapitalClient(config)
    try:
        capital.login()
    except Exception as exc:
        telegram.send_message(f"⚠️ Login Capital fallito: {exc}")
        return

    epic = _resolve_epic(signal_row, capital)
    if not epic:
        telegram.send_message(
            f"⚠️ Signal {signal_id}: epic per "
            f"{signal_row['asset']} non trovato"
        )
        db.update_signal_status(signal_id, "expired")
        return

    if sub == "exec":
        old_deal_id = args.get("old_deal_id")
        if old_deal_id:
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"⏳ <b>Rotation in corso</b>\n"
                    f"Chiusura posizione vecchia...",
                )
            try:
                close_position_by_deal_id(
                    capital,
                    db,
                    telegram,
                    old_deal_id,
                    reason=f"rotation->signal {signal_id}",
                    message_id=None,
                )
            except Exception as exc:
                log.exception("Chiusura rotation fallita")
                telegram.send_message(
                    f"⚠️ Chiusura fallita, non apro la nuova: {exc}"
                )
                return

    # Apertura nuova posizione (rot:exec dopo la chiusura, rot:open diretta).
    from .scanner import _direction_label

    current_price = _fetch_current_mid(capital, epic)
    if current_price:
        ok, reason = _check_entry_still_valid(
            signal_row, current_price, config.min_rr_at_entry
        )
        if not ok:
            db.update_signal_status(signal_id, "skipped")
            state.staged_budgets.pop(signal_id, None)
            msg = (
                f"⏭ <b>Rotation annullata</b> (signal {signal_id})\n\n"
                f"<b>{signal_row['asset']}</b>\n"
                f"Motivo: <i>{reason}</i>"
            )
            if message_id:
                telegram.edit_message_text(message_id, msg)
            else:
                telegram.send_message(msg)
            return

    if message_id:
        telegram.edit_message_text(
            message_id,
            f"⏳ <b>Apertura nuova posizione</b>\n"
            f"{signal_row['asset']} {_direction_label(signal_row['direction'], short=True)}...",
        )
    effective_budget = state.staged_budgets.get(
        signal_id, config.exposure_budget_eur
    )
    asset_features = {
        "epic": epic,
        "last_price": current_price or signal_row.get("entry_price"),
    }
    result = execute_signal(
        config,
        capital,
        db,
        signal_row,
        asset_features,
        exposure_override=effective_budget,
    )
    telegram.send_message(_format_execution_message(result, signal_row))
    state.staged_budgets.pop(signal_id, None)


def _format_execution_message(
    result: ExecutionResult, signal_row: dict[str, Any]
) -> str:
    from .scanner import _direction_label
    if result.executed:
        return (
            f"✅ <b>Posizione aperta su Capital.com</b>\n\n"
            f"<b>{signal_row['asset']}</b> "
            f"{_direction_label(signal_row['direction'], short=True)}\n"
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

    if action in ("mclose", "mhold"):
        _handle_monitor_callback(action, args, cb, config, db, telegram)
        return

    if action == "rot":
        _handle_rotation_callback(args, cb, config, db, telegram, state)
        return

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
            # 'manual_skipped' distingue il click utente da altri valori
            # storici (skipped/cancelled_*) per il logging Sprint 4.
            db.update_signal_status(signal_id, "manual_skipped")
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

            # Ricalcolo preview (margine, size, rischio) per il nuovo budget
            # e ri-edito SIA il testo del messaggio sia i bottoni, cosi'
            # l'utente vede subito cosa blocca realmente sul conto.
            sizing = _recompute_sizing_for_signal(
                config, signal_row, new_budget
            )
            try:
                telegram.edit_message_text(
                    message_id,
                    _build_confirm_text(signal_row, new_budget, sizing),
                )
            except Exception:
                log.exception(
                    "Ri-edit testo fallito per signal %s", signal_id
                )
            try:
                telegram.edit_message_reply_markup(
                    message_id,
                    _confirm_buttons(
                        signal_id, new_budget, config.budget_options
                    ),
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
        effective_exposure = state.staged_budgets.get(
            signal_id, args.get("inline_budget") or config.exposure_budget_eur
        )
        if message_id:
            telegram.edit_message_text(
                message_id,
                f"⏳ <b>Apertura posizione in corso</b> "
                f"(signal {signal_id}, esposizione €{effective_exposure:.0f})...",
            )

        capital = CapitalClient(config)
        try:
            capital.login()
        except Exception as exc:
            telegram.send_message(
                f"⚠️ Signal {signal_id}: login Capital fallito: {exc}"
            )
            return

        epic = _resolve_epic(signal_row, capital)
        if not epic:
            telegram.send_message(
                f"⚠️ Signal {signal_id}: epic per "
                f"{signal_row['asset']} non risolvibile"
            )
            db.update_signal_status(signal_id, "expired")
            return

        current_price = _fetch_current_mid(capital, epic)
        if current_price:
            ok, reason = _check_entry_still_valid(
                signal_row, current_price, config.min_rr_at_entry
            )
            if not ok:
                db.update_signal_status(signal_id, "skipped")
                state.staged_budgets.pop(signal_id, None)
                msg = (
                    f"⏭ <b>Apertura annullata</b> (signal {signal_id})\n\n"
                    f"<b>{signal_row['asset']}</b>\n"
                    f"Motivo: <i>{reason}</i>"
                )
                if message_id:
                    telegram.edit_message_text(message_id, msg)
                else:
                    telegram.send_message(msg)
                return

        asset_features = {
            "epic": epic,
            "last_price": current_price or signal_row.get("entry_price"),
        }
        result = execute_signal(
            config,
            capital,
            db,
            signal_row,
            asset_features,
            exposure_override=effective_exposure,
        )
        telegram.send_message(_format_execution_message(result, signal_row))
        state.staged_budgets.pop(signal_id, None)
