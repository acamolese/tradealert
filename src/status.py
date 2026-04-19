"""Composizione del messaggio per il comando Telegram /status.

Non esegue nuove scansioni: legge lo stato corrente (sessione, ultima
run dello scanner dal DB, posizioni live da Capital, segnali 24h) e
compone un riepilogo testuale.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .capital_client import CapitalClient
from .config import Config
from .db import Database
from .quiet_hours import (
    NIGHT_START_HOUR,
    WEEKDAY_START_HOUR,
    WEEKEND_START_HOUR,
    is_quiet_now,
)
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)

SCAN_MINUTES = (5, 35)

_OUTCOME_LABEL = {
    "no_data": "Nessun dato di mercato",
    "no_setup": "Nessun setup sopra soglia",
    "slots_full": "Slot pieni, rotation non applicabile",
    "rotation_proposed": "Inviata proposta di rotation",
    "signal_sent": "Inviato setup",
}


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _next_scan_at(now: datetime) -> datetime:
    """Prossima occorrenza cron (minuti 5/35 nella finestra attiva Rome)."""
    is_weekend = now.weekday() >= 5
    start_h = WEEKEND_START_HOUR if is_weekend else WEEKDAY_START_HOUR

    candidate = now.replace(second=0, microsecond=0)
    for _ in range(48):  # safety cap
        day_is_weekend = candidate.weekday() >= 5
        day_start = WEEKEND_START_HOUR if day_is_weekend else WEEKDAY_START_HOUR
        hour_f = candidate.hour + candidate.minute / 60
        # scan attivo solo dentro la finestra (stessa logica di quiet_hours)
        if day_start <= hour_f < NIGHT_START_HOUR:
            for m in SCAN_MINUTES:
                slot = candidate.replace(minute=m)
                if slot > now:
                    return slot
            # dopo minuto 35: prova la prossima ora
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
        # fuori finestra: salta alla prossima apertura
        if hour_f >= NIGHT_START_HOUR:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=0, minute=0
            )
            continue
        # prima dell'apertura: salta all'ora di start
        start_hour = int(day_start)
        start_min = int(round((day_start - start_hour) * 60))
        candidate = candidate.replace(hour=start_hour, minute=start_min)
        # ritorna il primo slot dopo 'now'
        for m in SCAN_MINUTES:
            slot = candidate.replace(minute=m)
            if slot > now:
                return slot
        candidate = (candidate + timedelta(hours=1)).replace(minute=0)
    # fallback improbabile
    return now + timedelta(hours=1)


def _format_run_line(run: dict[str, Any] | None, now_utc: datetime) -> str:
    if not run:
        return "Ultima scansione: <i>nessuna run registrata</i>"
    ran_at_raw = run.get("ran_at")
    try:
        ran_at = datetime.fromisoformat(ran_at_raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        ran_at = None
    ago = ""
    if ran_at:
        delta = now_utc - ran_at
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            ago = f"{minutes} min fa"
        else:
            ago = f"{minutes // 60}h {minutes % 60}m fa"
    outcome = run.get("outcome") or "?"
    label = _OUTCOME_LABEL.get(outcome, outcome)
    top_asset = run.get("top_asset")
    top_score = run.get("top_score")
    extra = ""
    if top_asset and top_score is not None:
        extra = f" (top: <b>{_esc(top_asset)}</b> score {top_score})"
    prefix = f"Ultima scansione ({ago})" if ago else "Ultima scansione"
    return f"{prefix}: {label}{extra}"


def _format_positions_block(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "📭 <b>Nessuna posizione aperta</b>"
    total_pnl = 0.0
    has_pnl = False
    lines: list[str] = []
    for wrapper in positions:
        pos = wrapper.get("position", {}) or {}
        market = wrapper.get("market", {}) or {}
        name = market.get("instrumentName") or pos.get("epic") or "?"
        direction = pos.get("direction", "?")
        pnl = pos.get("upl") or pos.get("profitAndLoss") or pos.get("pnl")
        if isinstance(pnl, (int, float)):
            total_pnl += float(pnl)
            has_pnl = True
            pnl_str = f"{pnl:+.2f}"
        else:
            pnl_str = "-"
        lines.append(
            f"• <b>{_esc(name)}</b> {direction} | P&amp;L: <code>{pnl_str}</code>"
        )
    header = f"📊 <b>Posizioni aperte ({len(positions)})</b>"
    if has_pnl:
        header += f" | Totale: <code>{total_pnl:+.2f}</code>"
    return header + "\n" + "\n".join(lines)


def _format_signals_block(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "🔕 <i>Nessun signal nelle ultime 24h</i>"
    lines = [f"🔔 <b>Signal ultime 24h ({len(signals)})</b>"]
    for s in signals[:5]:
        asset = s.get("asset") or "?"
        direction = (s.get("direction") or "").upper()
        score = s.get("score")
        status = s.get("status") or "?"
        lines.append(
            f"• <b>{_esc(asset)}</b> {direction} score {score} [{_esc(status)}]"
        )
    return "\n".join(lines)


def build_status_message(config: Config) -> str:
    rome = ZoneInfo("Europe/Rome")
    now_rome = datetime.now(rome)
    now_utc = datetime.now(timezone.utc)
    quiet = is_quiet_now(now_rome)

    is_weekend = now_rome.weekday() >= 5
    start = WEEKEND_START_HOUR if is_weekend else WEEKDAY_START_HOUR
    window = (
        f"finestra attiva {int(start):02d}:00-{int(NIGHT_START_HOUR):02d}:30 IT"
    )
    session_icon = "🌙" if quiet else "🟢"
    session_label = "Quiet hours (silenzio)" if quiet else "Sessione attiva"
    next_scan = _next_scan_at(now_rome)
    next_scan_str = next_scan.strftime("%a %H:%M")

    db = Database(config)
    last_run = None
    try:
        last_run = db.last_scanner_run()
    except Exception:
        log.exception("last_scanner_run fallito")
    try:
        signals_24h = db.recent_signals(24)
    except Exception:
        log.exception("recent_signals fallito")
        signals_24h = []

    positions: list[dict[str, Any]] = []
    positions_error: str | None = None
    try:
        capital = CapitalClient(config)
        capital.login()
        positions = capital.get_open_positions()
    except Exception as exc:
        log.exception("Fetch posizioni per /status fallito")
        positions_error = str(exc)

    parts = [
        f"{session_icon} <b>Stato TradeAlert</b>",
        f"Ora IT: <code>{now_rome.strftime('%Y-%m-%d %H:%M')}</code>",
        f"{session_label} ({window})",
        f"Prossimo scan: <code>{next_scan_str}</code>",
        "",
        _format_run_line(last_run, now_utc),
        "",
    ]
    if positions_error:
        parts.append(
            f"⚠️ <i>Impossibile leggere posizioni: {_esc(positions_error)}</i>"
        )
    else:
        parts.append(_format_positions_block(positions))
    parts.append("")
    parts.append(_format_signals_block(signals_24h))
    return "\n".join(parts)


def send_status(config: Config) -> None:
    telegram = TelegramClient(config)
    try:
        message = build_status_message(config)
    except Exception as exc:
        log.exception("build_status_message fallito")
        telegram.send_message(
            f"⚠️ <b>/status fallito</b>\n<i>{_esc(exc)}</i>"
        )
        return
    telegram.send_message(message)
