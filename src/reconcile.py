"""Reconcile: allinea lo stato dei trade nel DB con Capital.

Se una posizione e' chiusa su Capital (SL/TP colpito, chiusura manuale
dal frontend broker, close automatico) ma e' rimasta ``status='open'``
nel DB, il weekly report, l'analisi performance e il check
``max_open_positions`` leggono dati falsi.

Il reconcile:
1. Legge tutti i ``trades`` con ``status='open'`` dal DB.
2. Chiede a Capital le posizioni aperte in tempo reale.
3. Per ogni trade DB che non e' piu' sul broker, cerca la chiusura
   nella ``/history/activity`` (ultimi 30 giorni) e chiude il trade nel
   DB con close_price + pnl se disponibili, altrimenti lascia NULL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .db import Database

log = logging.getLogger(__name__)


# Keyword nella descrizione dell'activity che indicano una chiusura.
_CLOSE_DESCRIPTIONS = {
    "POSITION_CLOSED",
    "CLOSED",
    "STOP_ORDER_FILLED",
    "PROFIT_ORDER_FILLED",
    "PARTIALLY_CLOSED",
}


def _find_close_activity(
    activities: list[dict[str, Any]], deal_id: str
) -> dict[str, Any] | None:
    """Dalla history cerca l'activity di chiusura per un deal_id.
    Restituisce il dict dell'activity, oppure None."""
    for act in activities:
        if act.get("dealId") != deal_id:
            continue
        desc = (act.get("description") or "").upper()
        if any(k in desc for k in _CLOSE_DESCRIPTIONS):
            return act
        # fallback: details.reason
        details = act.get("details") or {}
        reason = (details.get("reason") or details.get("actionType") or "").upper()
        if any(k in reason for k in _CLOSE_DESCRIPTIONS):
            return act
    return None


def _extract_close_info(
    activity: dict[str, Any], db_trade: dict[str, Any]
) -> tuple[float | None, float | None, float | None, str]:
    """Ritorna (close_price, pnl, pnl_pct, exit_reason_tag).

    ``activity`` puo' avere struttura leggermente diversa a seconda del
    tier Capital; proviamo entrambe: ``level`` top-level o dentro
    ``details``. Se pnl non disponibile lo ricaviamo da
    (close-entry)*size * segno_direzione.
    """
    details = activity.get("details") or {}
    close_price = (
        activity.get("level")
        or details.get("level")
        or details.get("closeLevel")
    )
    pnl = details.get("amount") or activity.get("amount")
    direction = (db_trade.get("direction") or "").lower()
    entry = db_trade.get("entry_price")
    size = db_trade.get("size")

    # Fallback: calcola pnl e pnl_pct se abbiamo i dati
    if pnl is None and close_price and entry and size:
        delta = (float(close_price) - float(entry))
        if direction == "short":
            delta = -delta
        pnl = delta * float(size)
    pnl_pct = None
    if close_price and entry and direction:
        delta = (float(close_price) - float(entry)) / float(entry) * 100
        pnl_pct = -delta if direction == "short" else delta

    desc = (activity.get("description") or "").upper()
    if "STOP" in desc:
        tag = "reconcile:stop_hit"
    elif "PROFIT" in desc:
        tag = "reconcile:tp_hit"
    elif "CLOSED" in desc:
        tag = "reconcile:closed_on_broker"
    else:
        tag = "reconcile:activity_match"

    return (
        float(close_price) if close_price is not None else None,
        float(pnl) if pnl is not None else None,
        float(pnl_pct) if pnl_pct is not None else None,
        tag,
    )


def reconcile_open_trades(config: Config) -> dict[str, int]:
    """Allinea DB con broker. Ritorna contatori {checked, stale, closed,
    closed_with_pnl}. Loggare l'outcome e' compito del job entry point."""
    capital = CapitalClient(config)
    db = Database(config)

    capital.login()
    live_positions = capital.get_open_positions()
    live_deal_ids = {
        (p.get("position") or {}).get("dealId")
        for p in live_positions
    }
    live_deal_ids.discard(None)

    open_trades = db.get_open_trades()
    stale = [
        t for t in open_trades
        if t.get("capital_deal_id") and t["capital_deal_id"] not in live_deal_ids
    ]

    counters = {
        "checked": len(open_trades),
        "stale": len(stale),
        "closed": 0,
        "closed_with_pnl": 0,
    }

    if not stale:
        return counters

    # Fetch history una sola volta per tutti gli stale
    now = datetime.now(timezone.utc)
    try:
        activities = capital.get_activity_history(
            from_date=(now - timedelta(days=30)).isoformat(),
            to_date=now.isoformat(),
            detailed=True,
        )
    except (CapitalAPIError, Exception) as exc:
        log.warning(
            "Fetch history/activity fallito (%s): chiudo senza close_price/pnl",
            exc,
        )
        activities = []

    for trade in stale:
        deal_id = trade["capital_deal_id"]
        activity = _find_close_activity(activities, deal_id)
        if activity:
            close_price, pnl, pnl_pct, tag = _extract_close_info(
                activity, trade
            )
            if pnl is not None:
                counters["closed_with_pnl"] += 1
        else:
            close_price, pnl, pnl_pct, tag = None, None, None, "reconcile:no_match"
        try:
            db.close_trade(
                deal_id=deal_id,
                close_price=close_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=tag,
            )
            counters["closed"] += 1
            log.info(
                "Reconcile chiuso: %s (%s) close=%s pnl=%s tag=%s",
                trade["asset"],
                deal_id,
                close_price,
                pnl,
                tag,
            )
        except Exception:
            log.exception(
                "close_trade fallito per %s (%s)", trade["asset"], deal_id
            )
    return counters
