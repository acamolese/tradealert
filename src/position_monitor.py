"""Monitor sistematico delle posizioni aperte.

Passi per ogni posizione aperta:
1. ``_apply_trailing_stop``: sposta lo SL server-side se il profit in
   multipli R e' avanzato abbastanza (breakeven a 1R, +1R a 2R, ecc.).
2. ``_evaluate_position``: il LLM decide HOLD o CLOSE su thesis +
   feature aggiornate. Su CLOSE invia messaggio con bottoni (mclose/mhold).

I bottoni sono gestiti in modo asincrono dal listener daemon (vedi
``src.confirm_handler``), quindi qui niente polling inline.

Schedulato ogni 30 min in orario mercato.
"""

from __future__ import annotations

import json
import logging
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


def _apply_trailing_stop(
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    position: dict[str, Any],
) -> None:
    """Trailing stop R-multiple:
    - profit >= 1R -> SL a breakeven (entry)
    - profit >= 2R -> SL a entry + 1R (long) / entry - 1R (short)
    - profit >= NR -> SL a entry + (N-1)R
    Solo migliorativo: se il calcolo darebbe uno SL peggiore dell'attuale,
    non tocchiamo nulla. R e' derivato dallo stop originale del signal.
    """
    pos = position.get("position", {}) or {}
    market = position.get("market", {}) or {}
    deal_id = pos.get("dealId")
    direction = pos.get("direction")  # "BUY" | "SELL"
    entry = pos.get("level")
    current_sl = pos.get("stopLevel")
    asset_name = market.get("instrumentName") or pos.get("epic") or "?"

    if not (deal_id and direction and entry and current_sl):
        return

    trade = db.get_trade_by_deal_id(deal_id)
    signal = (
        db.get_signal(trade["signal_id"])
        if trade and trade.get("signal_id")
        else None
    )
    if not signal or not signal.get("stop_loss"):
        return

    stop_pct = float(signal["stop_loss"])
    entry = float(entry)
    current_sl = float(current_sl)
    if stop_pct <= 0:
        return

    r_distance = entry * stop_pct / 100

    snapshot = market.get("bid") or market.get("offer")
    if snapshot is None:
        try:
            m = capital.get_market(pos.get("epic"))
            snap = m.get("snapshot", {}) or {}
            bid = snap.get("bid")
            offer = snap.get("offer")
            current_price = (
                (float(bid) + float(offer)) / 2 if bid and offer else None
            )
        except Exception:
            current_price = None
    else:
        current_price = float(snapshot)

    if current_price is None:
        return

    if direction == "BUY":
        profit = current_price - entry
    else:
        profit = entry - current_price

    profit_r = profit / r_distance
    if profit_r < 1:
        return

    step = int(profit_r)  # quanti R completi
    offset_r = step - 1  # new SL a entry +/- offset_r * R

    if direction == "BUY":
        new_sl = entry + offset_r * r_distance
        if new_sl <= current_sl + 1e-9:
            return
    else:
        new_sl = entry - offset_r * r_distance
        if new_sl >= current_sl - 1e-9:
            return

    new_sl = round(new_sl, 5)
    try:
        capital.update_position(deal_id, stop_level=new_sl)
    except Exception as exc:
        log.warning("Trailing SL fallito per %s: %s", asset_name, exc)
        return

    log.info(
        "Trailing SL %s: %s -> %s (profit %.1fR, offset +%dR)",
        asset_name,
        current_sl,
        new_sl,
        profit_r,
        offset_r,
    )
    if trade:
        db.insert_monitoring_event(
            {
                "trade_id": trade["id"],
                "event_type": "trailing_sl",
                "reason": f"Profit {profit_r:.1f}R, SL +{offset_r}R dall'entry",
                "details": {
                    "old_sl": current_sl,
                    "new_sl": new_sl,
                    "current_price": current_price,
                    "r_distance": r_distance,
                    "profit_r": profit_r,
                },
            }
        )
    telegram.send_message(
        f"🛡 <b>Trailing SL</b> su {_esc(asset_name)}\n"
        f"Profit attuale: <code>{profit_r:.1f}R</code>\n"
        f"SL: <code>{current_sl}</code> → <code>{new_sl}</code>"
    )


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

    trade = db.get_trade_by_deal_id(deal_id) if deal_id else None
    thesis = ""
    if trade and trade.get("signal_id"):
        signal = db.get_signal(trade["signal_id"])
        if signal:
            thesis = signal.get("thesis", "")

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


def close_position_by_deal_id(
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    deal_id: str,
    reason: str,
    message_id: int | None = None,
) -> None:
    """Chiude una posizione per deal_id e persiste l'evento. Riutilizzabile
    sia dal monitor (quando l'LLM propone CLOSE) sia dal daemon (click
    ``mclose:`` dell'utente)."""
    try:
        close_resp = capital.close_position(deal_id)
        deal_ref = close_resp.get("dealReference")
        confirm = capital.confirm_deal(deal_ref) if deal_ref else {}
        close_level = float(confirm.get("level") or 0)
        pnl = confirm.get("profit") or confirm.get("profitAndLoss")
        pnl = float(pnl) if pnl is not None else None

        trade = db.get_trade_by_deal_id(deal_id)
        asset_name = trade.get("asset") if trade else deal_id
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
                exit_reason=f"manual:{reason[:80]}" if reason else "manual",
            )
            db.insert_monitoring_event(
                {
                    "trade_id": trade["id"],
                    "event_type": "manual_close",
                    "reason": reason,
                    "details": {"close_level": close_level, "pnl": pnl},
                }
            )

        text = (
            f"✅ <b>Posizione chiusa</b>\n"
            f"Asset: <b>{_esc(asset_name)}</b>\n"
            f"Prezzo chiusura: <code>{close_level}</code>\n"
            f"P&amp;L: <code>{pnl}</code>"
        )
        if message_id:
            telegram.edit_message_text(message_id, text)
        else:
            telegram.send_message(text)
    except Exception as exc:
        log.exception("Chiusura fallita")
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
        # 1. Trailing stop (server-side) prima della valutazione LLM
        try:
            _apply_trailing_stop(capital, db, telegram, position)
        except Exception:
            log.exception("Trailing SL fallito")

        # 2. Valutazione LLM per eventuale proposta di chiusura
        try:
            decision = _evaluate_position(config, capital, db, position)
        except Exception:
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

        deal_id = decision.get("deal_id")
        if not deal_id:
            continue

        # Invia proposta con bottoni mclose/mhold e ritorna.
        # Il daemon listener gestira' il click (confirm_handler).
        buttons = [
            [
                {
                    "text": "✅ Chiudi ora",
                    "callback_data": f"mclose:{deal_id}",
                },
                {
                    "text": "⏸ Lascia aperta",
                    "callback_data": f"mhold:{deal_id}",
                },
            ]
        ]
        telegram.send_message_with_buttons(
            _format_close_proposal(decision), buttons
        )
