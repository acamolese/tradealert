"""Backfill retroattivo del link signal->trade per orphan storici.

Caso d'uso: trade aperti dal bot in cui il bug #6 ha rotto il link
signal_id (vedi docs/bug-6-atomicita-execute-persist.md). Il monitor
li ha importati come orphan (signal_id=NULL, exit_reason inizia con
'manual_import:') ma in realta' sono trade generati da un signal di
cui non e' stato salvato il riferimento.

Strategia: per ogni orphan candidato, interroghiamo Capital activity
history nella finestra ±5min attorno a opened_at, troviamo l'attivita'
POSITION/ACCEPTED col deal_id corrispondente, e cerchiamo in signals
un record con stesso epic+direction e status compatibile creato in
finestra ±5min attorno all'apertura broker. Match univoco -> update
trades.signal_id + monitoring_event di audit. Match ambiguo o assente
-> log + skip (mai conferma utente, mai update parziale).

Esecuzione:
    python -m jobs.backfill_orphan_signal_links --dry-run
    python -m jobs.backfill_orphan_signal_links --limit 5
    python -m jobs.backfill_orphan_signal_links            # apply, no limit

NB: --dry-run NON scrive nulla; logga solo i match proposti.
Lo script e' idempotente: i trade gia' linkati vengono saltati,
i signal gia' executed_recovered restano tali.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from src.capital_client import CapitalAPIError, CapitalClient
from src.config import load_config
from src.db import Database

log = logging.getLogger(__name__)

# Finestre temporali per il matching.
#
# ACTIVITY_WINDOW_MIN: ricerca dell'activity Capital attorno a
# trade.opened_at. Capital tiene activity history per finestre brevi,
# qui ampliamo a 30min per gestire monitor che salta un ciclo
# (cron */30min).
#
# SIGNAL_WINDOW_BEFORE_MIN / SIGNAL_WINDOW_AFTER_MIN: finestra per
# cercare il signal compatibile attorno a trade.opened_at (UTC).
# Asimmetrica: il signal viene PRIMA della scoperta orphan, mai dopo.
# 60 min copre il caso peggiore (monitor cron */30 + finestra
# auto-confirm 60s + qualche secondo di network). Verso il futuro
# stringiamo a 5min: serve solo per gestire piccoli skew di clock.
#
# NB: signals.created_at e trade.opened_at sono entrambi in UTC.
# Capital activity.date e' in local timezone del server (CEST):
# non usato per il match temporale, solo per il match deterministico
# sul dealId.
ACTIVITY_WINDOW_MIN = 30
SIGNAL_WINDOW_BEFORE_MIN = 60
SIGNAL_WINDOW_AFTER_MIN = 5

# Statuti di signal che possiamo legittimamente promuovere a
# executed_recovered. cancelled_other e' lo status legacy pre-Tier 1.
# execute_inconsistent e' il nuovo status introdotto da Tier 1.
ELIGIBLE_SIGNAL_STATUSES = ("cancelled_other", "execute_inconsistent")

CAPITAL_DIRECTION_TO_LONG_SHORT = {"BUY": "long", "SELL": "short"}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _candidates_orphan_trades(db: Database) -> list[dict]:
    """Trade da considerare per il backfill. Filtri:
    - signal_id IS NULL (mai linkato)
    - capital_deal_id NOT NULL (servono per il match Capital).

    NOTA: non filtriamo su exit_reason perche' close_trade() sovrascrive
    quel campo alla chiusura (es. 'reconcile:stop_hit'), perdendo
    'manual_import:...' originale. signal_id IS NULL e' il marker
    affidabile: tutti i trade auto-execute hanno signal_id valido.
    L'unico falso positivo possibile sono aperture manuali dell'utente
    su Capital, ma per quelle il match _find_signal_candidates non
    trovera' alcun candidato e lo skip e' automatico.
    """
    response = (
        db._client.table("trades")
        .select("*")
        .is_("signal_id", "null")
        .not_.is_("capital_deal_id", "null")
        .order("opened_at", desc=True)
        .execute()
    )
    return response.data or []


def _find_activity_for_deal(
    capital: CapitalClient,
    deal_id: str,
    opened_at: datetime,
) -> dict | None:
    """Cerca in activity history la riga POSITION/ACCEPTED che ha
    aperto il deal_id. Finestra ±ACTIVITY_WINDOW_MIN attorno a
    opened_at del trade. Ritorna None se nessun match (deal troppo
    vecchio per lo storico Capital, o errore API)."""
    fro = _iso(opened_at - timedelta(minutes=ACTIVITY_WINDOW_MIN))
    to = _iso(opened_at + timedelta(minutes=ACTIVITY_WINDOW_MIN))
    try:
        activities = capital.get_activity_history(from_date=fro, to_date=to)
    except CapitalAPIError as exc:
        log.warning(
            "Capital activity history fallita per deal %s: %s",
            deal_id,
            exc,
        )
        return None
    for a in activities:
        if a.get("dealId") == deal_id and a.get("type") == "POSITION":
            return a
    return None


def _find_signal_candidates(
    db: Database,
    epic: str,
    direction_long_short: str,
    trade_opened_at: datetime,
) -> list[dict]:
    """Signal compatibili da promuovere. Filtro: status in eligible,
    stesso epic, stessa direction (long/short), created_at nella
    finestra [opened_at - SIGNAL_WINDOW_BEFORE_MIN,
              opened_at + SIGNAL_WINDOW_AFTER_MIN].

    Usiamo trade.opened_at (UTC) come ancora invece di
    Capital activity.date (timezone server, ambiguo)."""
    lo = (
        trade_opened_at - timedelta(minutes=SIGNAL_WINDOW_BEFORE_MIN)
    ).isoformat()
    hi = (
        trade_opened_at + timedelta(minutes=SIGNAL_WINDOW_AFTER_MIN)
    ).isoformat()
    response = (
        db._client.table("signals")
        .select("*")
        .in_("status", list(ELIGIBLE_SIGNAL_STATUSES))
        .eq("epic", epic)
        .eq("direction", direction_long_short)
        .gte("created_at", lo)
        .lte("created_at", hi)
        .order("created_at", desc=False)
        .execute()
    )
    return response.data or []


def _apply_link(
    db: Database, trade_id: int, signal_id: int, audit_details: dict
) -> None:
    """Scrive il backfill: trade.signal_id + signal.status +
    monitoring_event di audit. Niente Telegram (script one-shot)."""
    db._client.table("trades").update(
        {"signal_id": signal_id}
    ).eq("id", trade_id).execute()
    db.update_signal_status(signal_id, "executed_recovered")
    db.insert_monitoring_event(
        {
            "trade_id": trade_id,
            "event_type": "backfill_orphan_link",
            "reason": (
                f"Backfill: trade {trade_id} -> signal {signal_id}"
            ),
            "details": audit_details,
        }
    )


def run(dry_run: bool, limit: int | None) -> int:
    """Ritorna il numero di trade ricuciti (0 in dry-run)."""
    config = load_config()
    db = Database(config)
    capital = CapitalClient(config)
    capital.login()

    candidates = _candidates_orphan_trades(db)
    if limit is not None:
        candidates = candidates[:limit]

    log.info(
        "Backfill: %d trade orphan candidati (dry_run=%s)",
        len(candidates),
        dry_run,
    )

    applied = 0
    skipped_no_activity = 0
    skipped_no_signal = 0
    skipped_ambiguous = 0

    for trade in candidates:
        trade_id = trade["id"]
        deal_id = trade["capital_deal_id"]
        opened_at = _parse_iso(trade["opened_at"])

        activity = _find_activity_for_deal(capital, deal_id, opened_at)
        if not activity:
            log.info(
                "  trade #%s deal=%s: nessuna activity Capital nella "
                "finestra, skip",
                trade_id,
                deal_id,
            )
            skipped_no_activity += 1
            continue

        epic = activity.get("epic")
        details = activity.get("details") or {}
        direction_api = details.get("direction")
        direction_norm = CAPITAL_DIRECTION_TO_LONG_SHORT.get(
            direction_api or ""
        )

        if not (epic and direction_norm):
            log.warning(
                "  trade #%s: activity senza epic/direction, skip "
                "(epic=%r direction=%r)",
                trade_id,
                epic,
                direction_api,
            )
            skipped_no_signal += 1
            continue

        signals = _find_signal_candidates(
            db, epic, direction_norm, opened_at
        )
        if not signals:
            log.info(
                "  trade #%s (%s %s opened_at=%s): nessun signal "
                "eligibile, skip",
                trade_id,
                epic,
                direction_norm,
                opened_at.isoformat()[:19],
            )
            skipped_no_signal += 1
            continue

        if len(signals) > 1:
            ids = [s["id"] for s in signals]
            log.warning(
                "  trade #%s (%s %s opened_at=%s): match AMBIGUO su "
                "signals %s, skip per sicurezza",
                trade_id,
                epic,
                direction_norm,
                opened_at.isoformat()[:19],
                ids,
            )
            skipped_ambiguous += 1
            continue

        signal = signals[0]
        audit = {
            "deal_id": deal_id,
            "epic": epic,
            "direction": direction_norm,
            "trade_opened_at": trade["opened_at"],
            "capital_activity_date": activity["date"],
            "signal_created_at": signal["created_at"],
            "signal_status_before": signal["status"],
            "delta_signal_to_trade_opened_seconds": (
                opened_at - _parse_iso(signal["created_at"])
            ).total_seconds(),
            "backfilled_at": datetime.now(timezone.utc).isoformat(),
        }

        delta_s = audit["delta_signal_to_trade_opened_seconds"]
        if dry_run:
            log.info(
                "  [DRY-RUN] trade #%s -> signal #%s (%s %s, "
                "signal->opened delta=%.1fs, status %s->"
                "executed_recovered)",
                trade_id,
                signal["id"],
                epic,
                direction_norm,
                delta_s,
                signal["status"],
            )
            continue

        try:
            _apply_link(db, trade_id, signal["id"], audit)
            applied += 1
            log.info(
                "  APPLIED: trade #%s -> signal #%s (delta=%.1fs)",
                trade_id,
                signal["id"],
                delta_s,
            )
        except Exception:
            log.exception(
                "  ERROR applying backfill: trade #%s -> signal #%s",
                trade_id,
                signal["id"],
            )

    log.info(
        "Backfill done: applied=%d skipped_no_activity=%d "
        "skipped_no_signal=%d skipped_ambiguous=%d",
        applied,
        skipped_no_activity,
        skipped_no_signal,
        skipped_ambiguous,
    )
    return applied


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra i match proposti senza scrivere nulla",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limita il numero di orphan processati (default: tutti)",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
