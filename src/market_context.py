"""Market context: eventi macro critici e calendar economico.

Due sorgenti complementari:

1. ``get_critical_events``: legge ``config/critical_events.json``, un file
   che aggiorni a mano con eventi binari noti (summit, scadenze tregue)
   che il calendar economico standard non copre.

2. ``get_economic_calendar``: chiama Finnhub ``/calendar/economic`` e
   restituisce gli appuntamenti high-impact (FOMC, CPI, NFP, BCE, BoE,
   meeting banche centrali) nelle prossime ``hours_ahead`` ore. Se il
   tier Finnhub non ammette il calendar (free tier) o la chiave manca,
   ritorna silenziosamente lista vuota senza sollevare.

Entrambi i loader sono resilienti: se il file manca, l'API risponde
male, il JSON e' rotto, ecc., ritornano [] e loggano warning.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .config import Config

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


# ---------------- Finnhub economic calendar ----------------

_FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"


def _parse_calendar_time(value: str | None) -> datetime | None:
    """Formato Finnhub: '2026-04-23 18:00:00' in UTC."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def get_economic_calendar(
    config: Config,
    hours_ahead: int = 72,
    only_high_impact: bool = True,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Eventi macro ufficiali (FOMC, CPI, NFP, BCE, BoE...) dalle prossime
    ``hours_ahead`` ore. Richiede FINNHUB_API_KEY. Ritorna [] su qualsiasi
    errore (tier limitato, rete, API down) senza sollevare."""
    if not config.finnhub_api_key:
        return []
    ref_now = now or datetime.now(timezone.utc)
    try:
        r = requests.get(
            _FINNHUB_CALENDAR_URL,
            params={
                "from": ref_now.date().isoformat(),
                "to": (ref_now + timedelta(hours=hours_ahead))
                .date()
                .isoformat(),
                "token": config.finnhub_api_key,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        log.warning("Finnhub calendar: richiesta fallita: %s", exc)
        return []
    if not r.ok:
        log.warning(
            "Finnhub calendar HTTP %s (possibile restrizione del tier)",
            r.status_code,
        )
        return []
    try:
        data = r.json() or {}
    except ValueError:
        log.warning("Finnhub calendar: JSON malformato")
        return []

    events = data.get("economicCalendar") or []
    horizon = ref_now + timedelta(hours=hours_ahead)
    out: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        impact = (ev.get("impact") or "").lower()
        if only_high_impact and impact != "high":
            continue
        dt = _parse_calendar_time(ev.get("time"))
        if dt is None or dt < ref_now or dt > horizon:
            continue
        out.append(
            {
                "date": dt.isoformat(),
                "hours_until": round(
                    (dt - ref_now).total_seconds() / 3600, 1
                ),
                "event": ev.get("event") or "",
                "country": ev.get("country") or "",
                "impact": impact,
                "estimate": ev.get("estimate"),
                "prev": ev.get("prev"),
                "unit": ev.get("unit") or "",
            }
        )
    out.sort(key=lambda e: e["hours_until"])
    # Cap a 10 eventi: il prompt non deve gonfiarsi.
    return out[:10]
