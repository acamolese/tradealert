"""Logging high/low intra-trade per le posizioni aperte.

Prerequisito tecnico Sprint 3. Il trailing attuale logga eventi
``trailing_sl`` solo alle soglie discrete (0.5R, 1R, ...), che sottostima
il MFE vero (vedi docs/sprint2-mfe-analysis.md). Questo modulo registra a
ogni passata l'estremo di prezzo realmente raggiunto dall'apertura,
cumulativo, su un nuovo evento ``intra_trade_extreme``.

Per non perdere picchi tra una passata e l'altra (il monitor gira ~30m)
ad ogni run si leggono le candele MINUTE_5 dall'ultimo evento registrato
(o dall'apertura, alla prima passata) fino ad adesso, e si fondono i loro
estremi col cumulativo precedente.

Convenzione prezzi, coerente con jobs/peak_analysis.py:
  - ``high_seen``: max ``highPrice.bid``  (rilevante per il peak dei long)
  - ``low_seen`` : min ``lowPrice.ask``   (rilevante per il peak degli short)

Append-only: ogni passata inserisce un nuovo evento col cumulativo
aggiornato; l'ultimo evento di un trade contiene gli estremi finali.

NON modifica nulla del trailing live: è puro logging additivo. Da
schedulare separatamente (cron ~30m) oppure da invocare da
``monitor_positions`` a chiusura Sprint 2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .capital_client import CapitalClient
from .config import Config
from .db import Database
from .universe import UNIVERSE

log = logging.getLogger(__name__)


def _epic_for(asset_name: str) -> str | None:
    for a in UNIVERSE:
        if a.name == asset_name:
            return a.epic
    return None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _fetch_candles(capital: CapitalClient, epic: str, frm: datetime,
                   to: datetime, resolution: str = "MINUTE_5") -> list[dict]:
    """GET /prices con finestra from/to. Diretto sul session del client per
    non dipendere dalla firma di get_prices (che oggi non espone from/to)."""
    params = {
        "resolution": resolution,
        "from": frm.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": to.strftime("%Y-%m-%dT%H:%M:%S"),
        "max": 1000,
    }
    r = capital._session.get(
        capital._url(f"/prices/{epic}"),
        headers=capital._auth_headers(),
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("prices", [])


def _record_one(capital: CapitalClient, db: Database, position: dict[str, Any],
                now: datetime) -> dict | None:
    pos = position.get("position", {}) or {}
    market = position.get("market", {}) or {}
    deal_id = pos.get("dealId")
    if not deal_id:
        return None
    trade = db.get_trade_by_deal_id(deal_id)
    if not trade:
        return None  # posizione non ancora adottata: la salta, non e' compito suo

    asset = trade["asset"]
    epic = market.get("epic") or _epic_for(asset)
    if not epic:
        log.warning("intra_trade: epic non risolto per %s", asset)
        return None

    last = db.get_last_monitoring_event(trade["id"], "intra_trade_extreme")
    prev = (last.get("details") or {}) if last else {}
    high_seen = prev.get("high_seen")
    low_seen = prev.get("low_seen")

    # Finestra: dall'ultimo evento (o dall'apertura) ad adesso.
    if last and last.get("created_at"):
        frm = _parse(last["created_at"])
    else:
        frm = _parse(trade["opened_at"])

    try:
        candles = _fetch_candles(capital, epic, frm, now)
    except Exception:
        log.exception("intra_trade: fetch candele fallito per %s", asset)
        return None
    if not candles:
        return None

    win_high = max(float(c["highPrice"]["bid"]) for c in candles)
    win_low = min(float(c["lowPrice"]["ask"]) for c in candles)
    last_close = float(candles[-1]["closePrice"]["bid"])

    high_seen = win_high if high_seen is None else max(float(high_seen), win_high)
    low_seen = win_low if low_seen is None else min(float(low_seen), win_low)

    event = {
        "trade_id": trade["id"],
        "event_type": "intra_trade_extreme",
        "reason": f"high {high_seen:g} / low {low_seen:g} (cum dall'apertura)",
        "details": {
            "high_seen": high_seen,
            "low_seen": low_seen,
            "last_price": last_close,
            "window_from": frm.isoformat(),
            "window_candles": len(candles),
        },
    }
    db.insert_monitoring_event(event)
    log.info("intra_trade %s: high=%.5g low=%.5g (n=%d)",
             asset, high_seen, low_seen, len(candles))
    return event


def record_intra_trade_extremes(config: Config, capital: CapitalClient | None = None,
                                db: Database | None = None,
                                positions: list[dict] | None = None) -> int:
    """Registra l'estremo cumulativo per ogni posizione aperta. Ritorna il
    numero di eventi scritti. Riusa capital/db/positions se passati (per
    integrazione inline nel monitor senza HTTP duplicati)."""
    capital = capital or CapitalClient(config)
    db = db or Database(config)
    if positions is None:
        capital.login()
        positions = capital.get_open_positions()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    written = 0
    for p in positions or []:
        try:
            if _record_one(capital, db, p, now):
                written += 1
        except Exception:
            log.exception("intra_trade: record fallito")
    return written
