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
import math
from typing import Any

from anthropic import Anthropic

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .db import Database
from .features import compute_features
from .llm_usage import log_usage
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


def _resolve_tick_size(market: dict[str, Any]) -> float:
    """Ricava il tick size effettivo dello strumento.

    Preferenza: ``dealingRules.minStepDistance`` se in unita' POINTS
    (delta minimo accettato dal broker per stop/limit). Fallback:
    ``snapshot.decimalPlacesFactor`` come ``1 / 10**n``. Default 0.01.
    """
    rules = market.get("dealingRules") or {}
    msd = rules.get("minStepDistance") or {}
    if (msd.get("unit") == "POINTS") and msd.get("value"):
        try:
            v = float(msd["value"])
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    snap = market.get("snapshot") or {}
    dpf = snap.get("decimalPlacesFactor")
    try:
        if dpf is not None and int(dpf) >= 0:
            return 10.0 ** (-int(dpf))
    except (TypeError, ValueError):
        pass
    return 0.01


def _quantize_to_tick(value: float, tick: float) -> float:
    """Arrotonda ``value`` al multiplo piu' vicino di ``tick``.

    Il broker accetta solo valori multipli del tick, quindi qualunque
    valore intermedio viene riarrotondato lato server. Quantizzando in
    locale evitiamo che il confronto float new_sl vs current_sl
    (letto post-arrotondamento broker) generi loop sub-tick.
    """
    if tick <= 0:
        return value
    n_decimals = max(0, -int(round(math.log10(tick)))) if tick < 1 else 0
    return round(round(value / tick) * tick, n_decimals + 2)


def _apply_trailing_stop(
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    position: dict[str, Any],
    step_r: float = 0.5,
) -> None:
    """Trailing stop R-multiple. Logica:
    - profit >= 0.5R                     -> SL a entry +/- 0.5R (half-risk).
    - profit >= 1R                       -> SL a entry (breakeven).
    - poi step di ``step_r`` sopra il BE -> SL a entry + (n*step_r)
      con n = floor((profit_r - 1) / step_r).
    Esempio con step_r=0.5: 1R -> BE, 1.5R -> +0.5R, 2R -> +1R,
    2.5R -> +1.5R, ecc. Con step_r=1.0 (vecchio comportamento): 1R -> BE,
    2R -> +1R, 3R -> +2R.
    Solo migliorativo: se il nuovo SL e' peggiore dell'attuale non tocca.
    R e' derivato dallo stop originale del signal o dal trade orphan.
    """
    pos = position.get("position", {}) or {}
    market = position.get("market", {}) or {}
    deal_id = pos.get("dealId")
    direction = pos.get("direction")  # "BUY" | "SELL"
    entry = pos.get("level")
    current_sl = pos.get("stopLevel")
    epic = market.get("epic")  # epic sta in market, non in position
    asset_name = market.get("instrumentName") or epic or "?"

    if not (deal_id and direction and entry and current_sl):
        return

    trade = db.get_trade_by_deal_id(deal_id)

    # Posizione aperta fuori-bot (manuale su Capital): la importiamo nel
    # DB come orphan trade (signal_id=None), usando lo SL attuale come
    # riferimento per la R-distance. Dal prossimo ciclo il trailing
    # procede normalmente.
    if not trade:
        if not current_sl:
            log.warning(
                "Trailing SKIP: posizione manuale %s (%s) senza SL, "
                "impossibile derivare R-distance",
                deal_id,
                asset_name,
            )
            return
        try:
            size = pos.get("size") or 0
            direction_norm = "long" if direction == "BUY" else "short"
            profit_level = pos.get("profitLevel")
            trade = db.insert_trade(
                {
                    "signal_id": None,
                    "capital_deal_id": deal_id,
                    "asset": asset_name,
                    "direction": direction_norm,
                    "size": float(size),
                    "entry_price": float(entry),
                    "current_sl": float(current_sl),
                    "current_tp": (
                        float(profit_level) if profit_level else None
                    ),
                    "status": "open",
                    "exit_reason": "manual_import:trailing",
                }
            )
            log.warning(
                "Posizione manuale %s (%s) importata in trades come "
                "orphan: trailing attivo dal prossimo ciclo",
                deal_id,
                asset_name,
            )
        except Exception:
            log.exception(
                "Import orphan fallito per posizione %s", deal_id
            )
            return

    # Deriva lo stop_pct: se c'e un signal originale usa il suo, altrimenti
    # lo ricava dallo SL iniziale salvato sul trade (stabile nel tempo).
    signal = (
        db.get_signal(trade["signal_id"])
        if trade.get("signal_id") is not None
        else None
    )
    if signal and signal.get("stop_loss"):
        stop_pct = float(signal["stop_loss"])
    elif trade.get("entry_price") and trade.get("current_sl"):
        ref_entry = float(trade["entry_price"])
        ref_sl = float(trade["current_sl"])
        stop_pct = abs(ref_entry - ref_sl) / ref_entry * 100 if ref_entry else 0.0
    else:
        log.warning(
            "Trailing SKIP: stop_pct non derivabile per %s (%s)",
            deal_id,
            asset_name,
        )
        return
    entry = float(entry)
    current_sl = float(current_sl)
    if stop_pct <= 0:
        return

    r_distance = entry * stop_pct / 100

    # Fetch market completo (dealingRules + snapshot freschi) per ricavare
    # tick size e bid/offer aggiornati. Capital arrotonda lo stopLevel al
    # tick (es. Brent: 0.001 -> 3 decimali). Senza quantizzare al tick il
    # confronto float new_sl vs current_sl genera trigger sub-tick a ogni
    # ciclo (loop osservato su trade Brent #19 il 2026-04-28: 25+ trigger
    # in 4h con delta 0.00025 = un quarto di tick).
    market_full: dict[str, Any] = {}
    if epic:
        try:
            market_full = capital.get_market(epic) or {}
        except Exception:
            market_full = {}
    snap_full = market_full.get("snapshot") or {}

    bid = market.get("bid") or snap_full.get("bid")
    offer = market.get("offer") or snap_full.get("offer")
    if bid is not None and offer is not None:
        current_price = (float(bid) + float(offer)) / 2
    else:
        current_price = None

    if current_price is None:
        return

    tick_size = _resolve_tick_size(market_full)

    if direction == "BUY":
        profit = current_price - entry
    else:
        profit = entry - current_price

    profit_r = profit / r_distance
    if profit_r < 0.5:
        return

    # Half-risk fra 0.5R e 1R, poi step di step_r partendo da BE a 1R.
    if profit_r < 1:
        offset_r = -0.5  # SL a entry - 0.5R (long) / entry + 0.5R (short)
    else:
        # extra = quanto siamo sopra il breakeven, in unita' di R.
        extra = profit_r - 1.0
        n_steps = int(extra / step_r) if step_r > 0 else 0
        offset_r = n_steps * step_r

    if direction == "BUY":
        new_sl_raw = entry + offset_r * r_distance
    else:
        new_sl_raw = entry - offset_r * r_distance

    # Quantizzazione al tick e soglia minima di mezzo tick: skip update se
    # la differenza e' sotto un tick intero. Confronto post-quantizzazione
    # cosi' siamo in linea con cio' che il broker accetta come stopLevel.
    new_sl = _quantize_to_tick(new_sl_raw, tick_size)
    half_tick = tick_size / 2 if tick_size > 0 else 1e-9
    if direction == "BUY":
        if new_sl <= current_sl + half_tick:
            return
    else:
        if new_sl >= current_sl - half_tick:
            return

    # Dedup notifica: se l'ultimo trailing_sl scritto su monitoring_events
    # ha lo stesso new_sl entro tick, evita di rinotificare. Layer di
    # difesa aggiuntivo oltre alla quantizzazione (copre eventuali drift
    # di lettura SL tra Capital e DB).
    last_event = db.get_last_monitoring_event(
        trade["id"], "trailing_sl"
    ) if trade else None
    last_new_sl = (
        (last_event.get("details") or {}).get("new_sl")
        if last_event
        else None
    )
    suppress_telegram = (
        last_new_sl is not None
        and abs(float(last_new_sl) - new_sl) < tick_size
    )
    # Capital PUT /positions/{dealId} sostituisce i level non passati con
    # null (rimuove il TP). Per preservare il take profit dobbiamo SEMPRE
    # ripassarlo nel body. Sorgente: lo state live del broker
    # (``pos.profitLevel``) e' la verita' attuale; il DB ``trade.current_tp``
    # serve solo come backup se il broker l'ha gia' perso (in tal caso non
    # lo ripristiniamo, per non modificare retroattivamente posizioni
    # legacy aperte prima di questo fix).
    broker_tp = pos.get("profitLevel")
    profit_level_to_pass = float(broker_tp) if broker_tp else None
    try:
        capital.update_position(
            deal_id,
            stop_level=new_sl,
            profit_level=profit_level_to_pass,
        )
    except Exception as exc:
        log.warning("Trailing SL fallito per %s: %s", asset_name, exc)
        return

    tp_log = (
        f"TP {profit_level_to_pass:g} preserved"
        if profit_level_to_pass is not None
        else "broker TP=None (no preserve)"
    )
    log.info(
        "Trailing SL %s: %s -> %s (profit %.2fR, offset %+0.2fR) | %s",
        asset_name,
        current_sl,
        new_sl,
        profit_r,
        offset_r,
        tp_log,
    )
    reason_text = (
        f"Profit {profit_r:.2f}R, SL {offset_r:+.2f}R dall'entry"
    )
    if trade:
        db.insert_monitoring_event(
            {
                "trade_id": trade["id"],
                "event_type": "trailing_sl",
                "reason": reason_text,
                "details": {
                    "old_sl": current_sl,
                    "new_sl": new_sl,
                    "current_price": current_price,
                    "r_distance": r_distance,
                    "profit_r": profit_r,
                    "offset_r": offset_r,
                },
            }
        )
    if offset_r < 0:
        label = "half-risk (rischio dimezzato)"
    elif offset_r == 0:
        label = "breakeven (rischio zero)"
    else:
        label = f"+{offset_r}R in profitto"
    if suppress_telegram:
        log.info(
            "Trailing notifica soppressa per %s: stesso SL entro tick "
            "rispetto a ultimo trailing_sl loggato",
            asset_name,
        )
    else:
        telegram.send_message(
            f"🛡 <b>Trailing SL</b> su {_esc(asset_name)}\n"
            f"Profit: <code>{profit_r:.2f}R</code> → SL {label}\n"
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
    epic = market.get("epic")  # epic sta in market.epic, non in position.epic
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

    # Monitor task: HOLD/CLOSE su una singola posizione, payload piccolo
    # e output strutturato corto. Haiku basta e taglia ~80% del costo
    # rispetto a Sonnet sulle ~16 chiamate giornaliere in finestra attiva.
    client = Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.anthropic_model_fast,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
    )
    log_usage(config, "monitor", response)
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


def _resolve_close_price_from_activity(
    capital: CapitalClient,
    deal_id: str,
    db_trade: dict[str, Any],
    retries: int = 3,
    sleep_sec: float = 1.0,
) -> float | None:
    """Cerca nell'activity history il prezzo di esecuzione del counter-trade
    di chiusura. ``confirm_deal`` su Capital, per un close manuale di
    posizione, ritorna in ``level`` l'entry della posizione originale
    (non il prezzo del counter-trade).

    Riutilizza la helper ``_find_close_activity`` di ``reconcile.py``
    (logica unica per riconoscere counter-trade strutturali) e ne legge
    il ``close_price`` via ``_extract_close_info``. Cosi' i due path
    di chiusura DB (close_position_by_deal_id e reconcile) condividono
    lo stesso parser e qualunque fix futuro vale per entrambi.

    Capital ha lag di 1-2s tra DELETE /positions e visibilita'
    nell'activity: retry breve per assorbirlo.
    """
    import time
    from .reconcile import _find_close_activity, _extract_close_info

    for attempt in range(retries):
        try:
            activities = capital.get_activity_history(
                last_period_sec=600, detailed=True
            )
        except Exception:
            activities = []
        found = _find_close_activity(activities, deal_id, db_trade=db_trade)
        if found:
            close_price, _pnl, _pct, _tag = _extract_close_info(
                found, db_trade
            )
            if close_price:
                return close_price
        if attempt < retries - 1:
            time.sleep(sleep_sec)
    return None


def _resolve_close_price_from_market(
    capital: CapitalClient, epic: str | None, direction_word: str
) -> float | None:
    """Subordinato: se l'activity non risponde, usa il prezzo a cui il
    broker chiuderebbe ora (bid se chiudiamo un long, offer se chiudiamo
    uno short) come migliore approssimazione del fill effettivo."""
    if not epic:
        return None
    try:
        snap = (capital.get_market(epic) or {}).get("snapshot") or {}
    except Exception:
        return None
    if direction_word == "long":
        price = snap.get("bid")
    else:
        price = snap.get("offer")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


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
        # Cattura entry e direzione PRIMA del close, per i fallback di
        # close_level e pnl quando confirm_deal non li popola correttamente.
        trade = db.get_trade_by_deal_id(deal_id)
        direction_word = (
            (trade.get("direction") or "").lower() if trade else ""
        )
        entry = float(trade.get("entry_price") or 0) if trade else 0.0
        size = float(trade.get("size") or 0) if trade else 0.0
        signal = (
            db.get_signal(trade["signal_id"])
            if trade and trade.get("signal_id")
            else None
        )
        epic = (signal or {}).get("epic")

        close_resp = capital.close_position(deal_id)
        deal_ref = close_resp.get("dealReference")
        confirm = capital.confirm_deal(deal_ref) if deal_ref else {}
        confirm_level = float(confirm.get("level") or 0)
        pnl_raw = confirm.get("profit") or confirm.get("profitAndLoss")
        pnl = float(pnl_raw) if pnl_raw is not None else None

        # confirm.level e' inaffidabile per close manuali (restituisce
        # l'entry originale). Sorgente primaria: activity history (stessa
        # helper usata da reconcile). Sorgente subordinata: bid/offer
        # current. Ultima risorsa: confirm_level.
        activity_level = _resolve_close_price_from_activity(
            capital, deal_id, trade or {}
        )
        market_level = (
            _resolve_close_price_from_market(capital, epic, direction_word)
            if activity_level is None
            else None
        )
        close_level = activity_level or market_level or confirm_level
        close_source = (
            "activity"
            if activity_level
            else ("market" if market_level else "confirm")
        )

        # Se Capital non ha popolato pnl, ricavalo da (close-entry)*size
        # con segno per direzione. Currency-blind come il resto del sistema.
        if pnl is None and close_level and entry and size:
            delta = close_level - entry
            if direction_word == "short":
                delta = -delta
            pnl = round(delta * size, 4)

        asset_name = trade.get("asset") if trade else deal_id
        if trade:
            pnl_pct = (
                ((close_level - entry) / entry * 100) if entry else None
            )
            if direction_word == "short" and pnl_pct is not None:
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
                    "details": {
                        "close_level": close_level,
                        "pnl": pnl,
                        "close_source": close_source,
                        "confirm_level": confirm_level,
                    },
                }
            )

        log.info(
            "Close %s: source=%s close_level=%s confirm_level=%s pnl=%s",
            asset_name,
            close_source,
            close_level,
            confirm_level,
            pnl,
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


def run_trailing_stops(config: Config) -> None:
    """Versione leggera del monitor: applica solo il trailing stop a
    tutte le posizioni aperte. Nessuna chiamata LLM, solo aritmetica e
    ``update_position`` quando uno SL va spostato. Pensato per girare
    spesso (ogni 5 min, H24) per proteggere rapidamente il breakeven
    senza incidere sui costi.

    NIENTE check quiet_hours: la protezione dello SL e' migliorativa
    per definizione (uno SL viene spostato solo se piu' protettivo del
    precedente) e crypto/forex sono aperti H24, quindi blocchiamo solo
    quando non c'e' niente da fare (no posizioni)."""
    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)

    capital.login()
    positions = capital.get_open_positions()
    if not positions:
        return

    log.info(
        "Trailing scan su %d posizioni (step_r=%.2f)",
        len(positions),
        config.trailing_step_r,
    )
    for position in positions:
        try:
            _apply_trailing_stop(
                capital, db, telegram, position, step_r=config.trailing_step_r
            )
        except Exception:
            log.exception("Trailing SL fallito")


def monitor_positions(config: Config) -> None:
    if is_quiet_now():
        log.info("Skip monitor: %s", quiet_reason())
        return

    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)

    capital.login()
    open_positions = capital.get_open_positions()

    # Reconcile inline: chiude in DB i trade che il broker ha gia' chiuso
    # (stop, tp, chiusura manuale dal frontend). Riusa la sessione Capital
    # e le live_positions appena recuperate per evitare HTTP duplicati.
    # Il job h24 jobs.reconcile resta come safety net fuori dalla finestra
    # attiva del monitor (notte e prima/dopo 7-22).
    try:
        from .reconcile import reconcile_open_trades

        rec = reconcile_open_trades(
            config,
            capital=capital,
            db=db,
            live_positions=open_positions,
            telegram=telegram,
        )
        if rec.get("closed", 0) > 0:
            log.info(
                "[reconcile] inline: checked=%d stale=%d closed=%d with_pnl=%d",
                rec.get("checked", 0),
                rec.get("stale", 0),
                rec.get("closed", 0),
                rec.get("closed_with_pnl", 0),
            )
    except Exception:
        log.exception("Reconcile inline fallito (continuo col monitor)")

    if not open_positions:
        log.info("Nessuna posizione aperta, nulla da monitorare")
        return

    log.info("Monitor su %d posizioni aperte", len(open_positions))

    for position in open_positions:
        # 1. Trailing stop (server-side) prima della valutazione LLM
        try:
            _apply_trailing_stop(
                capital, db, telegram, position, step_r=config.trailing_step_r
            )
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

        # Non proporre CLOSE se il mercato non e' attualmente tradeable:
        # l'utente non potrebbe eseguire nemmeno cliccando, e la proposta
        # tornerebbe a ogni run finche' il mercato riapre (loop di alert).
        market = position.get("market", {}) or {}
        market_status = (market.get("marketStatus") or "").upper()
        if market_status and market_status not in ("TRADEABLE",):
            log.info(
                "Skip alert CLOSE su %s: mercato %s, attendere riapertura",
                decision.get("asset"),
                market_status,
            )
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
