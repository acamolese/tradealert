"""Market context: eventi macro critici gestiti a mano.

Leggi ``config/critical_events.json`` e restituisci gli eventi nelle
prossime ``hours_ahead`` ore (default 72). Il formato e' stabile, il file
resta nel repo e va aggiornato quando si sa di scadenze binarie rilevanti
(summit, tregue, FOMC, CPI, NFP, BCE, earnings major).

Il loader e' resiliente: se il file manca, e' malformato, o ``events`` e'
vuoto, ritorna una lista vuota senza sollevare.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "critical_events.json"
)


def _parse_date(value: Any) -> datetime | None:
    """Accetta ISO-8601 con 'Z' o offset esplicito. None se parse fallisce."""
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_critical_events(
    hours_ahead: int = 72,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Eventi con ``date`` tra adesso e adesso+``hours_ahead``.

    Gli eventi gia' passati vengono scartati. Il campo ``_comment`` e
    ``_example`` del file sono ignorati (non hanno ``date`` parsabile).
    """
    p = path or _DEFAULT_PATH
    if not p.exists():
        log.info("critical_events: file non trovato in %s", p)
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("critical_events: parse fallito (%s)", exc)
        return []
    events = data.get("events") or []
    if not isinstance(events, list):
        return []

    ref_now = now or datetime.now(timezone.utc)
    horizon = ref_now + timedelta(hours=hours_ahead)
    out: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        dt = _parse_date(ev.get("date"))
        if dt is None or dt < ref_now or dt > horizon:
            continue
        out.append(
            {
                "date": dt.isoformat(),
                "hours_until": round(
                    (dt - ref_now).total_seconds() / 3600, 1
                ),
                "description": ev.get("description", ""),
                "impact_assets": ev.get("impact_assets") or [],
                "direction_hint": ev.get("direction_hint", "unknown"),
            }
        )
    out.sort(key=lambda e: e["hours_until"])
    return out
