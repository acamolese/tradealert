"""Monitor sistematico delle posizioni aperte.

Per ogni posizione aperta, ricalcola feature tecniche aggiornate, le manda
al LLM con la thesis originale del trade e chiede una decisione: HOLD,
CLOSE, o (futuro) SWITCH. Notifica Telegram solo se serve agire (no spam).

Schedulato ogni 30 min in orario mercato.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from anthropic import Anthropic

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .db import Database
from .features import compute_features
from .quiet_hours import is_quiet_now, quiet_reason
from .telegram_client import TelegramClient
from .universe import UNIVERSE

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Sei il risk manager di un bot di swing trading retail.
Per ogni posizione aperta ricevi: dettagli del trade (asset, direzione,
entry, stop, take profit, P&L corrente, thesis originale del setup),
feature tecniche aggiornate (RSI, ATR, trend, distanza da massimi/minimi).

Decidi UNA azione tra:
- HOLD: la posizione va come previsto, niente da fare
- CLOSE: chiudi anticipatamente. Motivi validi: thesis invalidata,
  momentum invertito contro la posizione, livello tecnico chiave violato
  contro il trade, R:R residuo peggiorato sotto 1:1

Sii conservativo. Default HOLD nel dubbio. Proponi CLOSE solo se hai
segnali tecnici CHIARI di invalidazione. Non sovra-reagire al rumore di
qualche candela.

Rispondi SOLO con JSON valido, niente testo prima o dopo:
{
  "action": "HOLD" | "CLOSE",
  "reason": "spiegazione breve in italiano (max 2 righe)",
  "urgency": "low" | "medium" | "high"
}"""


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_epic(asset_name: str) -> str | None:
    for asset in UNIVERSE:
        if asset.name == asset_name:
            return asset.epic
    return None


def _evaluate_position(
    config: Config,
    capital: CapitalClient,
    db: Database,
    position: dict[str, Any],
) -> dict[str, Any] | None:
    """Ritorna {action, reason, urgency, ...} o None se non valutabile."""
    pos = position.get("position", {}) or {}
    market = position.get("market", {}) or {}
    deal_id = pos.get("dealId")
    epic = pos.get("epic")
    direction = pos.get("direction")
    entry = pos.get("level")
    size = pos.get("size")
    pnl = pos.get("upl") or pos.get("profitAndLoss") or pos.get("pnl")
    asset_name = market.get("instrumentName") or epic

    if not (epic and direction and entry):
        log.warning("Posizione %s senza dati sufficienti", deal_id)
        return None

    # Recupera trade dal DB per la thesis originale (se trovato)
    trade = db.get_trade_by_deal_id(deal_id) if deal_id else None
    thesis = ""
    if trade and trade.get("signal_id"):
        signal = db.get_signal(trade["signal_id"])
        if signal:
            thesis = signal.get("thesis", "")

    # Fetch dati aggiornati per re-evaluation
    try:
        candles = capital.get_prices(epic, resolution="HOUR_4", max_bars=60)
        snapshot = capital.get_market(epic)
    except CapitalAPIError as exc:
        log.warning("Skip valutazione %s: %s", asset_name, exc)
        return None

    features = compute_features(asset_name, candles, snapshot=snapshot)
    current_price = features.get("last_price")
    current_pnl_pct = (
        ((current_price - entry) / entry * 100)
        if current_price and direction == "BUY"
        else ((entry - current_price) / entry * 100) if current_price else None
    )

    payload = {
        "position": {
            "asset": asset_name,
            "direction": direction,
            "entry_price": entry,
            "current_price": current_price,
            "stop_loss": pos.get("stopLevel"),
            "take_profit": pos.get("profitLevel"),
            "size": size,
            "pnl_currency": pnl,
            "pnl_pct_estimate": current_pnl_pct,
            "thesis_originale": thesis,
        },
        "current_features": features,
    }

    client = Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("LLM JSON parse fail: %s | raw=%r", exc, raw[:200])
        return None

    decision["deal_id"] = deal_id
    decision["asset"] = asset_name
    decision["current_price"] = current_price
    decision["pnl_pct"] = current_pnl_pct
    return decision


def _format_close_proposal(decision: dict[str, Any]) -> str:
    urgency_icon = {"high": "🚨", "medium": "⚠️", "low": "ℹ️"}.get(
        decision.get("urgency", "low"), "ℹ️"
    )
    pnl = decision.get("pnl_pct")
    pnl_str = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "-"
    return (
        f"{urgency_icon} <b>Proposta di chiusura</b>\n\n"
        f"<b>{_esc(decision['asset'])}</b>\n"
        f"P&amp;L attuale: <code>{pnl_str}</code>\n"
        f"Prezzo: <code>{decision.get('current_price')}</code>\n\n"
        f"<i>{_esc(decision.get('reason', ''))}</i>"
    )


def _close_position_safely(
    config: Config,
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    deal_id: str,
    decision: dict[str, Any],
    message_id: int | None,
) -> None:
    try:
        close_resp = capital.close_position(deal_id)
        deal_ref = close_resp.get("dealReference")
        confirm = capital.confirm_deal(deal_ref) if deal_ref else {}
        close_level = float(confirm.get("level") or 0)
        pnl = confirm.get("profit") or confirm.get("profitAndLoss")
        pnl = float(pnl) if pnl is not None else None

        trade = db.get_trade_by_deal_id(deal_id)
        if trade:
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
                exit_reason=f"monitor:{decision.get('reason','')[:80]}",
            )
            db.insert_monitoring_event(
                {
                    "trade_id": trade["id"],
                    "event_type": "monitor_close",
                    "reason": decision.get("reason", ""),
                    "details": {
                        "close_level": close_level,
                        "pnl": pnl,
                        "urgency": decision.get("urgency"),
                    },
                }
            )

        text = (
            f"✅ <b>Posizione chiusa su tua autorizzazione</b>\n"
            f"Asset: <b>{_esc(decision['asset'])}</b>\n"
            f"Prezzo chiusura: <code>{close_level}</code>\n"
            f"P&amp;L: <code>{pnl}</code>"
        )
        if message_id:
            telegram.edit_message_text(message_id, text)
        else:
            telegram.send_message(text)
    except Exception as exc:
        log.exception("Chiusura monitor fallita")
        telegram.send_message(
            f"⚠️ <b>Chiusura fallita</b>\n<i>{_esc(exc)}</i>"
        )


def monitor_positions(config: Config) -> None:
    if is_quiet_now():
        log.info("Skip monitor: %s", quiet_reason())
        return

    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)

    capital.login()
    open_positions = capital.get_open_positions()

    if not open_positions:
        log.info("Nessuna posizione aperta, nulla da monitorare")
        return

    log.info("Monitor su %d posizioni aperte", len(open_positions))

    for position in open_positions:
        try:
            decision = _evaluate_position(config, capital, db, position)
        except Exception as exc:
            log.exception("Valutazione posizione fallita")
            continue

        if not decision:
            continue
        action = (decision.get("action") or "").upper()
        log.info(
            "Decision %s su %s: %s (%s)",
            action,
            decision.get("asset"),
            decision.get("urgency"),
            decision.get("reason", "")[:80],
        )
        if action != "CLOSE":
            continue

        # CLOSE: notifica con bottoni e attendi click
        deal_id = decision.get("deal_id")
        if not deal_id:
            continue

        buttons = [[
            {
                "text": "✅ Chiudi ora",
                "callback_data": f"mclose:{deal_id}",
            },
            {
                "text": "⏸ Lascia aperta",
                "callback_data": f"mhold:{deal_id}",
            },
        ]]
        start_offset = telegram.drain_updates()
        sent = telegram.send_message_with_buttons(
            _format_close_proposal(decision), buttons
        )
        message_id = sent.get("message_id")

        deadline = time.time() + min(config.confirm_timeout_sec, 240)
        while True:
            remaining = int(deadline - time.time())
            if remaining <= 0:
                if message_id:
                    telegram.edit_message_text(
                        message_id,
                        f"⌛ <b>Proposta di chiusura scaduta</b>\n"
                        f"Posizione lasciata aperta. Sara' rivalutata al prossimo monitor.",
                    )
                break
            data, _, start_offset = telegram.wait_for_callback(
                valid_prefixes=(f"mclose:{deal_id}", f"mhold:{deal_id}"),
                timeout_sec=remaining,
                start_offset=start_offset,
            )
            if data is None:
                continue
            action_kind = data.split(":")[0]
            if action_kind == "mhold":
                if message_id:
                    telegram.edit_message_text(
                        message_id,
                        f"⏸ <b>Posizione lasciata aperta</b>\n"
                        f"<i>{_esc(decision.get('reason',''))}</i>",
                    )
                break
            # mclose
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"⏳ <b>Chiusura in corso...</b>",
                )
            _close_position_safely(
                config, capital, db, telegram, deal_id, decision, message_id
            )
            break
