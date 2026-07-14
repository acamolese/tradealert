"""Sprint 7 Fase A — selettore d'entrata deterministico in SHADOW.

LOGGING-ONLY: a ogni scan, dopo il pre-filtro, registra cosa un picker SENZA
LLM avrebbe scelto sugli stessi candidati. Lo scan reale (LLM) prosegue
invariato. Non viola "un esperimento alla volta": non agisce.

Regola PRE-REGISTRATA (v1-momentum, docs/sprint7-fase-a-entry-shadow.md,
da non ritoccare durante la raccolta):
  - candidati tradeable con spread <= 0.5% e |daily_pct_change| >= 0.5%;
  - pick = massimo |daily_pct_change| (il mover piu' forte del giorno);
  - direzione = segno del momentum (continuazione);
  - stop = max(1.5 * atr_pct, 0.5%), target = 2 * stop.

Record in ``monitoring_events`` con event_type='entry_shadow' (trade_id NULL).
Fine Fase A: gestita da jobs/sprint6_agenda.py (avviso Telegram automatico).
Rollback: ENTRY_SHADOW=false nel .env (default: attivo).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .db import Database

log = logging.getLogger(__name__)

RULE_VERSION = "v1-momentum"
MIN_ABS_DAILY_PCT = 0.5
MAX_SPREAD_PCT = 0.5
STOP_ATR_MULT = 1.5
MIN_STOP_PCT = 0.5
TARGET_RR = 2.0


def deterministic_pick(features: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Il setup che il picker senza LLM aprirebbe, o None se nessuno."""
    best: dict[str, Any] | None = None
    for asset, f in features.items():
        try:
            dpc = float(f["daily_pct_change"])
            spread = float(f.get("spread_pct") or 0.0)
            atrp = float(f.get("atr_pct_of_price") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if f.get("market_status") not in ("TRADEABLE", "EDITS_ONLY"):
            continue
        if spread > MAX_SPREAD_PCT or abs(dpc) < MIN_ABS_DAILY_PCT:
            continue
        if best is None or abs(dpc) > abs(best["daily_pct_change"]):
            stop = max(STOP_ATR_MULT * atrp, MIN_STOP_PCT)
            best = {
                "asset": asset,
                "direction": "long" if dpc > 0 else "short",
                "daily_pct_change": round(dpc, 3),
                "spread_pct": round(spread, 4),
                "stop_pct": round(stop, 3),
                "target_pct": round(TARGET_RR * stop, 3),
            }
    return best


def deterministic_proposals(features: dict[str, dict[str, Any]]) -> list:
    """Sprint 7 Fase B: la regola v1-momentum come lista di SetupProposal.

    Estensione DICHIARATA della regola di Fase A (docs/sprint7-fase-b.md):
    non solo il pick n.1 ma la lista dei candidati validi ordinata per |dpc|
    decrescente, cosi' dedup 24h / budget / cap a valle possono far scendere
    ai successivi, esattamente come con le proposte LLM. Il primo della
    lista e' identico al pick di Fase A.

    Score sintetico 7.0 + min(|dpc|/10, 0.9): monotono in |dpc| (preserva il
    ranking), nella banda tipica LLM (7.0-7.5), sopra MIN_SCORE_THRESHOLD=7
    e azzerabile dalla penalita' -2 dei guardrail macro (che restano attivi).
    La thesis inizia con "[v1-momentum]": marker usato dall'agenda per il
    gate forward dei primi 20 trade.
    """
    from .llm_analyzer import SetupProposal

    out: list = []
    for asset, f in features.items():
        try:
            dpc = float(f["daily_pct_change"])
            spread = float(f.get("spread_pct") or 0.0)
            atrp = float(f.get("atr_pct_of_price") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if f.get("market_status") not in ("TRADEABLE", "EDITS_ONLY"):
            continue
        if spread > MAX_SPREAD_PCT or abs(dpc) < MIN_ABS_DAILY_PCT:
            continue
        stop = max(STOP_ATR_MULT * atrp, MIN_STOP_PCT)
        direction = "long" if dpc > 0 else "short"
        out.append(
            SetupProposal(
                asset=asset,
                direction=direction,
                score=round(7.0 + min(abs(dpc) / 10.0, 0.9), 2),
                thesis=(
                    f"[v1-momentum] Mover piu' forte del giorno: {dpc:+.2f}% "
                    f"(spread {spread:.2f}%). Continuazione {direction}, "
                    f"stop 1.5xATR ({stop:.2f}%), target 2x. "
                    "Selettore deterministico Fase B, nessuno scoring LLM."
                ),
                suggested_stop_pct=round(stop, 3),
                suggested_target_pct=round(TARGET_RR * stop, 3),
            )
        )
    out.sort(key=lambda p: p.score, reverse=True)
    return out


def log_entry_shadow(db: Database, filtered_features: dict[str, dict[str, Any]]) -> None:
    """Scrive il pick shadow per questo scan. Best-effort: mai sollevare."""
    if os.environ.get("ENTRY_SHADOW", "true").strip().lower() != "true":
        return
    pick = deterministic_pick(filtered_features)
    db.insert_monitoring_event(
        {
            "trade_id": None,
            "event_type": "entry_shadow",
            "reason": "Fase A: pick deterministico (logging-only)",
            "details": {
                "rule": RULE_VERSION,
                "n_candidates": len(filtered_features),
                "candidates": sorted(filtered_features.keys()),
                "pick": pick,
            },
        }
    )
    log.info(
        "Entry shadow: %s",
        f"{pick['asset']} {pick['direction']} (dpc {pick['daily_pct_change']}%)"
        if pick else "nessun pick",
    )
