"""Wrapper Supabase per le operazioni MVP su signals/trades/snapshots.

Usa la service_role key se configurata (bypassa RLS, corretto per
processi server-side come questo bot). Fallback alla anon key per
non rompere installazioni vecchie, ma in produzione con RLS abilitato
SOLO la service_role key permette letture/scritture: l'anon key
restituirebbe sempre liste vuote o errori 401.
"""

from __future__ import annotations

import logging
from typing import Any

from supabase import Client, create_client

from .config import Config

log = logging.getLogger(__name__)


class Database:
    def __init__(self, config: Config) -> None:
        key = config.supabase_service_role_key or config.supabase_anon_key
        if not config.supabase_service_role_key:
            log.warning(
                "Database: SUPABASE_SERVICE_ROLE_KEY non configurata, uso "
                "anon key. Con RLS abilitato le scritture FALLIRANNO."
            )
        self._client: Client = create_client(config.supabase_url, key)

    def insert_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        response = self._client.table("signals").insert(signal).execute()
        return response.data[0]

    def update_signal_status(self, signal_id: int, status: str) -> None:
        self._client.table("signals").update({"status": status}).eq(
            "id", signal_id
        ).execute()

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        response = (
            self._client.table("signals")
            .select("*")
            .eq("id", signal_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    def get_open_trades(self) -> list[dict[str, Any]]:
        response = (
            self._client.table("trades")
            .select("*")
            .eq("status", "open")
            .execute()
        )
        return response.data

    def insert_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        response = self._client.table("trades").insert(trade).execute()
        return response.data[0]

    def get_trade_by_deal_id(self, deal_id: str) -> dict[str, Any] | None:
        response = (
            self._client.table("trades")
            .select("*")
            .eq("capital_deal_id", deal_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    def close_trade(
        self,
        deal_id: str,
        close_price: float | None,
        pnl: float | None,
        pnl_pct: float | None,
        exit_reason: str,
    ) -> None:
        from datetime import datetime, timezone

        self._client.table("trades").update(
            {
                "status": "closed",
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "close_price": close_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason,
            }
        ).eq("capital_deal_id", deal_id).execute()

    def insert_account_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._client.table("account_snapshots").insert(snapshot).execute()

    def insert_monitoring_event(self, event: dict[str, Any]) -> None:
        self._client.table("monitoring_events").insert(event).execute()

    def insert_scanner_run(self, row: dict[str, Any]) -> dict[str, Any]:
        """Traccia l'esito di una run dello scanner per /status."""
        response = self._client.table("scanner_runs").insert(row).execute()
        return response.data[0] if response.data else {}

    def last_scanner_run(self) -> dict[str, Any] | None:
        response = (
            self._client.table("scanner_runs")
            .select("*")
            .order("ran_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    def recent_signals(self, hours: int = 24) -> list[dict[str, Any]]:
        """Signal completi creati nelle ultime ``hours`` (qualsiasi status)."""
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        response = (
            self._client.table("signals")
            .select("asset,direction,score,status,created_at")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []

    def recent_signal_assets(self, hours: int = 24) -> set[str]:
        """Asset per cui e' stato creato un signal nelle ultime ``hours``
        (qualsiasi status). Usato per evitare di riproporre ripetutamente
        lo stesso asset in scan successivi dello stesso giorno."""
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        response = (
            self._client.table("signals")
            .select("asset")
            .gte("created_at", cutoff)
            .execute()
        )
        return {row["asset"] for row in (response.data or []) if row.get("asset")}


# ---------------------------------------------------------------------------
# Risk cap helpers (drawdown settimanale)
# ---------------------------------------------------------------------------
# Funzioni modulo (non metodi della classe Database) per separare la logica
# di risk dal CRUD generico. Firma `(db, ...)` perche' la chiamata e'
# concettualmente "leggi dati di risk dal DB", non un'operazione interna.


def weekly_realized_pnl(db: Database, hours: int = 168) -> float:
    """Somma del campo ``pnl`` dei trade chiusi nelle ultime ``hours`` ore
    (default 7 giorni = 168 ore, finestra rolling).

    Esclude esplicitamente i trade ancora aperti tramite filtro
    ``status='closed'``: il pnl flottante delle posizioni aperte NON
    contribuisce al cap di drawdown realizzato. Filtra inoltre per
    ``closed_at >= now() - hours`` (PostgREST scarta automaticamente
    i record con closed_at NULL).

    Trade con ``pnl IS NULL`` contano 0 (record legacy con solo pnl_pct):
    il drawdown cap monitora perdite realmente registrate, non ricostruite.

    Ritorna float: negativo per perdita, positivo per profitto, 0.0 se
    nessun trade chiuso nella finestra.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    response = (
        db._client.table("trades")
        .select("pnl")
        .eq("status", "closed")
        .gte("closed_at", cutoff)
        .execute()
    )
    total = 0.0
    for row in response.data or []:
        pnl = row.get("pnl")
        if pnl is not None:
            total += float(pnl)
    return total


def risk_cap_notified_today(db: Database) -> bool:
    """True se una notifica risk_cap e' gia' stata registrata in
    monitoring_events oggi (UTC, dalle 00:00 di oggi).

    Usato come anti-spam Telegram: la notifica scatta una sola volta
    per giorno solare anche se il cron di scanner gira N volte. I run
    successivi loggano comunque l'auto-stop ma non rinotificano.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    response = (
        db._client.table("monitoring_events")
        .select("id")
        .eq("event_type", "risk_cap_notified")
        .gte("created_at", start_of_day.isoformat())
        .limit(1)
        .execute()
    )
    return bool(response.data)


def record_risk_cap_notification(
    db: Database, weekly_pnl: float, cap_eur: float
) -> None:
    """Registra in monitoring_events l'invio della notifica risk_cap.
    trade_id resta NULL (la colonna e' nullable). details conserva i
    valori per audit/diagnostica."""
    db.insert_monitoring_event(
        {
            "event_type": "risk_cap_notified",
            "reason": "weekly_drawdown_cap",
            "details": {
                "weekly_pnl_eur": round(weekly_pnl, 2),
                "cap_eur": cap_eur,
            },
        }
    )
