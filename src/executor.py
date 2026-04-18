"""Esecuzione di un signal su Capital.com.

Safety hardcoded:
- Esegue solo se EXECUTION_ENABLED e' true.
- Rifiuta se ci sono gia' MAX_OPEN_POSITIONS posizioni aperte.
- Stop loss obbligatorio (server-side).
- Sizing calcolato con risk_pct sul balance corrente; se la size minima del
  broker richiederebbe piu' rischio del budget, salta.
- Logga ogni decisione (eseguita o saltata) come monitoring_event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .capital_client import CapitalClient
from .config import Config
from .db import Database
from .risk import calculate_size

log = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    executed: bool
    deal_id: str | None = None
    trade_id: int | None = None
    reason: str = ""
    size: float | None = None
    entry_price: float | None = None
    stop_level: float | None = None
    profit_level: float | None = None


def _market_meta(market: dict[str, Any]) -> dict[str, float]:
    """Estrae min size, step, margin factor e regole di stop/profit distance."""
    rules = market.get("dealingRules", {}) or {}
    min_size_field = rules.get("minDealSize", {}) or {}
    min_size = float(min_size_field.get("value", 0.01) or 0.01)

    snapshot = market.get("snapshot", {}) or {}
    step_field = rules.get("minSizeIncrement") or {}
    size_step = float(step_field.get("value", min_size) or min_size)

    instrument = market.get("instrument", {}) or {}
    margin_factor_pct = float(instrument.get("marginFactor", 5) or 5)
    margin_factor = margin_factor_pct / 100.0

    bid = snapshot.get("bid")
    offer = snapshot.get("offer")
    mid_price = (
        (float(bid) + float(offer)) / 2 if bid and offer else None
    )

    # Distanze minime per stop e take profit (in punti, non percentuale).
    # Capital le esprime in "minControlledRiskStopDistance" / "minStopOrProfitDistance".
    min_stop_field = (
        rules.get("minStopOrProfitDistance")
        or rules.get("minControlledRiskStopDistance")
        or {}
    )
    min_stop_distance = float(min_stop_field.get("value", 0) or 0)
    stop_unit = min_stop_field.get("unit", "POINTS")

    return {
        "min_size": min_size,
        "size_step": size_step,
        "margin_factor": margin_factor,
        "mid_price": mid_price,
        "min_stop_distance": min_stop_distance,
        "stop_unit": stop_unit,
    }


def _enforce_min_distance(
    direction: str,
    entry_price: float,
    stop_level: float,
    profit_level: float | None,
    min_distance: float,
    unit: str,
) -> tuple[float, float | None]:
    """Garantisce che SL/TP rispettino la min distance dichiarata da Capital.

    unit puo' essere "POINTS" (assoluto, in unita' di prezzo) o "PERCENTAGE".
    """
    if min_distance <= 0:
        return stop_level, profit_level

    if unit == "PERCENTAGE":
        min_abs = entry_price * min_distance / 100
    else:
        min_abs = min_distance

    if direction == "BUY":
        if entry_price - stop_level < min_abs:
            stop_level = entry_price - min_abs
        if profit_level is not None and profit_level - entry_price < min_abs:
            profit_level = entry_price + min_abs
    else:  # SELL
        if stop_level - entry_price < min_abs:
            stop_level = entry_price + min_abs
        if profit_level is not None and entry_price - profit_level < min_abs:
            profit_level = entry_price - min_abs

    return stop_level, profit_level


def execute_signal(
    config: Config,
    capital: CapitalClient,
    db: Database,
    signal_row: dict[str, Any],
    asset_features: dict[str, Any],
    exposure_override: float | None = None,
) -> ExecutionResult:
    epic = asset_features.get("epic")
    if not epic:
        return ExecutionResult(False, reason="epic mancante nel signal")

    direction_word = signal_row["direction"].upper()
    direction_api = "BUY" if direction_word == "LONG" else "SELL"

    # 1. Limite posizioni aperte
    open_positions = capital.get_open_positions()
    if len(open_positions) >= config.max_open_positions:
        return ExecutionResult(
            False,
            reason=f"Posizioni aperte {len(open_positions)} >= max {config.max_open_positions}",
        )

    # 2. Snapshot mercato per regole di sizing e prezzo aggiornato
    market = capital.get_market(epic)
    meta = _market_meta(market)
    entry_price = meta["mid_price"] or asset_features.get("last_price")
    if not entry_price:
        return ExecutionResult(False, reason="Prezzo di mercato non disponibile")

    # 3. Stop e target dal signal (in % rispetto a entry)
    stop_pct = float(signal_row.get("stop_loss") or 0)
    target_pct = float(signal_row.get("take_profit") or 0)
    if stop_pct <= 0:
        return ExecutionResult(False, reason="Stop loss % non definito nel signal")

    # 4. Capitale di riferimento per il sizing.
    # Usiamo "available" (margine libero) come proxy di capitale di rischio:
    # se hai posizioni aperte altrove o conto piccolo, il sizing si adegua.
    accounts = capital.get_account_info().get("accounts", [])
    available_margin: float | None = None
    if accounts:
        bal = accounts[0].get("balance", {}) or {}
        available_margin = float(bal.get("available") or 0) or None
    if not available_margin or available_margin <= 0:
        return ExecutionResult(
            False, reason="Margine disponibile zero o non leggibile"
        )

    effective_exposure = exposure_override or config.exposure_budget_eur
    sizing = calculate_size(
        exposure_budget=effective_exposure,
        entry_price=entry_price,
        margin_factor=meta["margin_factor"],
        min_size=meta["min_size"],
        size_step=meta["size_step"],
        stop_pct=stop_pct,
        available_margin=available_margin,
    )
    if sizing.size is None:
        return ExecutionResult(False, reason=f"Sizing rifiutato: {sizing.reason}")

    # 5. Calcolo livelli assoluti SL e TP, poi applico min distance del broker
    if direction_api == "BUY":
        stop_level = entry_price * (1 - stop_pct / 100)
        profit_level = (
            entry_price * (1 + target_pct / 100) if target_pct > 0 else None
        )
    else:
        stop_level = entry_price * (1 + stop_pct / 100)
        profit_level = (
            entry_price * (1 - target_pct / 100) if target_pct > 0 else None
        )

    stop_level, profit_level = _enforce_min_distance(
        direction_api,
        entry_price,
        stop_level,
        profit_level,
        meta.get("min_stop_distance", 0),
        meta.get("stop_unit", "POINTS"),
    )

    # 6. Apertura posizione
    try:
        deal_resp = capital.create_position(
            epic=epic,
            direction=direction_api,
            size=sizing.size,
            stop_level=round(stop_level, 5),
            profit_level=round(profit_level, 5) if profit_level else None,
        )
    except Exception as exc:
        # Se e' un CapitalAPIError ha gia' il body dettagliato.
        return ExecutionResult(
            False, reason=f"create_position fallita: {exc}"
        )

    deal_reference = deal_resp.get("dealReference")
    if not deal_reference:
        return ExecutionResult(False, reason="dealReference mancante in risposta")

    # 7. Conferma esito (Capital lavora in modo asincrono)
    try:
        confirm = capital.confirm_deal(deal_reference)
    except Exception as exc:
        return ExecutionResult(
            False, reason=f"confirm_deal fallita: {exc}"
        )

    status = confirm.get("dealStatus") or confirm.get("status")
    # Il dealId della POSIZIONE (utile per close) e' in affectedDeals[0].dealId.
    # confirm.dealId e' invece l'ID dell'ordine, che Capital non accetta su DELETE.
    affected = confirm.get("affectedDeals") or []
    deal_id = (affected[0].get("dealId") if affected else None) or confirm.get(
        "dealId"
    )
    fill_level = float(confirm.get("level") or entry_price)

    if status != "ACCEPTED":
        return ExecutionResult(
            False,
            reason=f"Deal non accettato: status={status}, reason={confirm.get('reason')}",
        )

    # Verifica che la posizione sia effettivamente leggibile, e in caso
    # rimpiazza il deal_id col vero position id.
    try:
        for pos_wrapper in capital.get_open_positions():
            pos = pos_wrapper.get("position", {}) or {}
            if pos.get("dealReference") == deal_reference:
                deal_id = pos.get("dealId") or deal_id
                break
    except Exception as exc:
        log.warning("Lookup posizione post-apertura fallito: %s", exc)

    # 8. Persistenza trade + signal status
    trade_row = db.insert_trade(
        {
            "signal_id": signal_row["id"],
            "capital_deal_id": deal_id,
            "asset": signal_row["asset"],
            "direction": signal_row["direction"],
            "size": sizing.size,
            "entry_price": fill_level,
            "current_sl": round(stop_level, 5),
            "current_tp": round(profit_level, 5) if profit_level else None,
            "status": "open",
        }
    )
    db.update_signal_status(signal_row["id"], "executed")
    db.insert_monitoring_event(
        {
            "trade_id": trade_row["id"],
            "event_type": "opened",
            "reason": "Esecuzione automatica da signal",
            "details": {
                "deal_reference": deal_reference,
                "deal_id": deal_id,
                "fill_level": fill_level,
                "size": sizing.size,
                "notional": sizing.notional,
                "margin_estimate": sizing.margin_estimate,
                "risk_estimate": sizing.risk_estimate,
                "exposure_budget": effective_exposure,
            },
        }
    )

    return ExecutionResult(
        executed=True,
        deal_id=deal_id,
        trade_id=trade_row["id"],
        size=sizing.size,
        entry_price=fill_level,
        stop_level=round(stop_level, 5),
        profit_level=round(profit_level, 5) if profit_level else None,
    )
