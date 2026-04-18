"""Wrapper Supabase per le operazioni MVP su signals/trades/snapshots."""

from __future__ import annotations

from typing import Any

from supabase import Client, create_client

from .config import Config


class Database:
    def __init__(self, config: Config) -> None:
        self._client: Client = create_client(
            config.supabase_url, config.supabase_anon_key
        )

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
        close_price: float,
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
