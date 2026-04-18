"""Regole orarie di silenzio: no notifiche di notte e (nel weekend)
prima dell'orario operativo del broker.

Default:
- lun-ven: attivo 07:00 - 22:30 IT
- sab-dom: attivo 09:00 - 22:30 IT (Capital apre i mercati weekend dalle 9)
- fuori da queste finestre: i job devono uscire silenziosi.

Le regole girano sull'orario di Rome (Europe/Rome), che gestisce
automaticamente il cambio ora legale.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

WEEKDAY_START_HOUR = 7.0   # 07:00
WEEKEND_START_HOUR = 9.0   # 09:00
NIGHT_START_HOUR = 22.5    # 22:30


def is_quiet_now(now: datetime | None = None) -> bool:
    """True se siamo nella fascia di silenzio (no notifiche)."""
    now = now or datetime.now(ZoneInfo("Europe/Rome"))
    is_weekend = now.weekday() >= 5
    start = WEEKEND_START_HOUR if is_weekend else WEEKDAY_START_HOUR
    current = now.hour + now.minute / 60
    return current < start or current >= NIGHT_START_HOUR


def quiet_reason(now: datetime | None = None) -> str:
    """Spiegazione testuale del motivo di silenzio (per i log)."""
    now = now or datetime.now(ZoneInfo("Europe/Rome"))
    is_weekend = now.weekday() >= 5
    start = WEEKEND_START_HOUR if is_weekend else WEEKDAY_START_HOUR
    return (
        f"quiet_hours: ora IT={now.strftime('%H:%M')}, "
        f"weekend={is_weekend}, attivo da {start:.1f} a {NIGHT_START_HOUR:.1f}"
    )
