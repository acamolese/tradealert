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

    def trades_risk_columns_available(self) -> bool:
        """True se la migration 20260702150000 (risk_at_open_eur/exit_r) e'
        applicata. Rilevamento runtime con cache per processo: il codice che
        scrive le nuove colonne resta inerte finche' lo schema non le ha,
        cosi' l'ordine deploy codice / migration e' libero (Sprint 6 A4.1)."""
        cached = getattr(self, "_risk_cols_available", None)
        if cached is None:
            try:
                self._client.table("trades").select(
                    "risk_at_open_eur"
                ).limit(1).execute()
                cached = True
            except Exception:
                cached = False
            self._risk_cols_available = cached
        return cached

    # ---------- signal_to_trade_link (bug #6 Tier 2) ----------
    #
    # Le scritture qui sono best-effort: una loro failure non deve mai
    # impedire la prosecuzione di execute_signal. Sono telemetria
    # strutturata, non sorgente di verita'. La sorgente di verita' resta
    # ``trades``. Il loro valore e': se l'executor crasha fra
    # create_position e insert_trade, il link esiste gia' con il
    # capital_deal_id reale, e il monitor puo' usarlo per ricostruire
    # il link senza euristiche.

    def link_attempt_start(self, signal_id: int) -> None:
        """Upsert link a status='attempting' subito prima di chiamare
        Capital. Upsert per gestire retry manuali sullo stesso signal."""
        self._client.table("signal_to_trade_link").upsert(
            {"signal_id": signal_id, "status": "attempting"},
            on_conflict="signal_id",
        ).execute()

    def link_attempt_capital_open(
        self, signal_id: int, capital_deal_id: str
    ) -> None:
        """create_position ok + deal_id determinato. Da qui il monitor
        sa il signal_id per qualunque deal_id che dovesse risultare
        orfano lato Capital."""
        self._client.table("signal_to_trade_link").update(
            {
                "status": "capital_open",
                "capital_deal_id": capital_deal_id,
            }
        ).eq("signal_id", signal_id).execute()

    def link_attempt_persisted(self, signal_id: int) -> None:
        """insert_trade riuscito: il link ha gia' fatto il suo lavoro.
        Status terminale, niente cleanup necessario."""
        self._client.table("signal_to_trade_link").update(
            {"status": "persisted"}
        ).eq("signal_id", signal_id).execute()

    def link_attempt_failed(self, signal_id: int, error_text: str) -> None:
        """Eccezione catturata. Marker per indagine retroattiva. Lo stato
        finale potrebbe diventare 'persisted' se il monitor poi
        ricuce il link."""
        self._client.table("signal_to_trade_link").update(
            {
                "status": "failed",
                "error_text": (error_text or "")[:2000],
            }
        ).eq("signal_id", signal_id).execute()

    def find_link_by_deal_id(
        self, capital_deal_id: str
    ) -> dict[str, Any] | None:
        """Match deterministico per il fast path del monitor. Ritorna il
        link se esiste un record con quel capital_deal_id, in qualsiasi
        status (incluso 'persisted', utile per debug). Il chiamante deve
        filtrare 'persisted' se vuole solo i pending."""
        response = (
            self._client.table("signal_to_trade_link")
            .select("*")
            .eq("capital_deal_id", capital_deal_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    def find_inconsistent_signal(
        self,
        epic: str,
        direction: str,
        window_minutes: int = 10,
    ) -> list[dict[str, Any]]:
        """Cerca signal con status='execute_inconsistent' su epic+direction
        creati nelle ultime ``window_minutes``.

        Usato dal position_monitor per ricucire orphan trade al signal
        che li ha generati quando l'auto-executor crasha fra
        create_position su Capital e insert_trade su Supabase
        (bug #6 atomicita').

        Ritorna lista vuota se nessun match, lista con 1 elemento se
        match univoco, lista con >1 elementi se ambiguo (in quel caso
        il chiamante deve fallback su orphan classico per sicurezza).
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat()
        response = (
            self._client.table("signals")
            .select("*")
            .eq("status", "execute_inconsistent")
            .eq("epic", epic)
            .eq("direction", direction)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []

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

        update = {
            "status": "closed",
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "close_price": close_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
        }
        # Sprint 6 A4.1: exit_r = pnl / rischio all'apertura. Best-effort:
        # nessuna failure qui deve impedire la chiusura del trade a DB.
        if pnl is not None and self.trades_risk_columns_available():
            try:
                row = (
                    self._client.table("trades")
                    .select("risk_at_open_eur")
                    .eq("capital_deal_id", deal_id)
                    .limit(1)
                    .execute()
                    .data
                )
                risk = row[0].get("risk_at_open_eur") if row else None
                if risk and float(risk) > 0:
                    update["exit_r"] = round(float(pnl) / float(risk), 4)
            except Exception:
                pass
        self._client.table("trades").update(update).eq(
            "capital_deal_id", deal_id
        ).execute()

    def insert_account_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._client.table("account_snapshots").insert(snapshot).execute()

    def insert_monitoring_event(self, event: dict[str, Any]) -> None:
        self._client.table("monitoring_events").insert(event).execute()

    def get_last_monitoring_event(
        self, trade_id: int, event_type: str
    ) -> dict[str, Any] | None:
        """Ultimo monitoring_event per (trade_id, event_type) o None."""
        response = (
            self._client.table("monitoring_events")
            .select("*")
            .eq("trade_id", trade_id)
            .eq("event_type", event_type)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

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

    def insert_llm_usage(self, row: dict[str, Any]) -> None:
        """Traccia tokens e costo di una chiamata Anthropic."""
        self._client.table("llm_usage").insert(row).execute()

    def llm_cost_by_month(self, months: int = 3) -> list[dict[str, Any]]:
        """Spesa Anthropic stimata raggruppata per mese (UTC), dal piu'
        recente. Restituisce al massimo ``months`` righe con chiavi
        ``month`` ("YYYY-MM"), ``cost_usd`` (float), ``calls`` (int).

        Non aggreghiamo lato DB con una view per restare nello stack
        Python-only: il volume di righe (poche centinaia al mese) e'
        trascurabile.
        """
        from collections import defaultdict
        from datetime import datetime, timedelta, timezone

        # Finestra ampia: ultimi N+1 mesi solari per coprire il bordo.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=31 * (months + 1))
        ).isoformat()
        response = (
            self._client.table("llm_usage")
            .select("ran_at,cost_usd")
            .gte("ran_at", cutoff)
            .execute()
        )
        buckets: dict[str, dict[str, float]] = defaultdict(
            lambda: {"cost_usd": 0.0, "calls": 0}
        )
        for row in response.data or []:
            ran_at = row.get("ran_at") or ""
            month = ran_at[:7]  # "YYYY-MM"
            if not month:
                continue
            buckets[month]["cost_usd"] += float(row.get("cost_usd") or 0)
            buckets[month]["calls"] += 1
        sorted_months = sorted(buckets.keys(), reverse=True)[:months]
        return [
            {
                "month": m,
                "cost_usd": round(buckets[m]["cost_usd"], 4),
                "calls": int(buckets[m]["calls"]),
            }
            for m in sorted_months
        ]

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


def sprint2_short_signal_count(db: Database, start_iso: str) -> int:
    """Numero di signal con ``direction='short'`` generati da ``start_iso``
    in poi, a PRESCINDERE dallo status (executed, skipped, cancelled,
    expired).

    Misura la capacita' del sistema di PROPORRE short, non quanti si
    concretizzano in trade: l'esecuzione dipende da filtri RR, cap
    settimanale, conferma utente e condizioni di mercato, non dal bias
    diagnosticato. Usato dal kill switch direzionale Sprint 2.
    """
    response = (
        db._client.table("signals")
        .select("id")
        .eq("direction", "short")
        .gte("created_at", start_iso)
        .execute()
    )
    return len(response.data or [])


def sprint2_kill_notified(
    db: Database, event_type: str = "sprint2_directional_kill"
) -> bool:
    """True se una notifica del kill switch direzionale di tipo
    ``event_type`` e' gia' stata registrata in monitoring_events. La
    notifica va inviata una sola volta (lo stato del kill non oscilla
    avanti e indietro), non una al giorno come per il risk_cap.

    Due event_type usati dal guard:
      - 'sprint2_directional_kill'        kill reale, scanner fermo
      - 'sprint2_directional_kill_paused' falso positivo, pausa cautelativa
    """
    response = (
        db._client.table("monitoring_events")
        .select("id")
        .eq("event_type", event_type)
        .limit(1)
        .execute()
    )
    return bool(response.data)


def sprint2_discarded_short_proposals(
    db: Database, start_iso: str, min_score: float
) -> int:
    """Numero di run dello scanner da ``start_iso`` in poi in cui almeno
    una proposta del LLM era uno short con score >= ``min_score`` ma NON
    e' diventata un signal (soppressa da dedup 24h, slot pieni,
    market_status, ecc.).

    Legge ``scanner_runs.notes.proposals`` (la sintesi loggata da
    ``_proposals_summary``). Conta al massimo una volta per run. Serve al
    kill switch direzionale per distinguere un kill reale (il sistema non
    vede gli short) da un falso positivo (li vede e propone, ma i filtri
    operativi li sopprimono prima che diventino signal).
    """
    response = (
        db._client.table("scanner_runs")
        .select("notes")
        .gte("ran_at", start_iso)
        .execute()
    )
    runs = 0
    for row in response.data or []:
        proposals = (row.get("notes") or {}).get("proposals") or []
        for p in proposals:
            if (p.get("direction") or "").lower() != "short":
                continue
            try:
                score = float(p.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if score >= min_score:
                runs += 1
                break  # una sola proposta short di qualita' per run
    return runs
