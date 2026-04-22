"""Guardrail deterministici post-LLM per eventi macro e correlazione.

Il system prompt chiede al LLM di ridurre score, allargare stop e
scartare setup esposti a eventi binari. In pratica il LLM a volte
ignora le regole se vede un setup tecnico forte. Qui applichiamo le
stesse regole in codice Python dopo che il LLM ha prodotto i
proposal, cosi' l'output non puo' piu' bypassarle.

Due gruppi di regole:

1. Eventi macro (critical_events + economic_calendar):
   - Se un asset e' esposto a evento binario (direction_hint
     ``risk_off_if_fails`` / ``risk_on_if_fails``) entro 24h e lo
     score e' < 8: il proposal viene SCARTATO.
   - Se direction_hint opposta alla direzione proposta: score -= 2.
   - Se esposto a un qualsiasi evento high-impact entro 24h:
     ``suggested_stop_pct`` moltiplicato per 1.5.

2. Correlazione con posizioni aperte:
   - Se il candidato e' nello stesso macro_group (risk_on / risk_off)
     di una posizione gia' aperta: score -= 1.5. Evita di raddoppiare
     la stessa scommessa macro (es. Viridien + Nasdaq entrambi
     risk_on).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .llm_analyzer import SetupProposal


# Mapping asset name -> macro group (risk_on / risk_off / neutro).
# Gli asset non in mappa vengono considerati "other" e non generano
# penalita' di correlazione (comportamento conservativo).
MACRO_GROUPS: dict[str, str] = {
    # Risk-off: store of value, commodity difensive, safe haven FX
    "Gold": "risk_off",
    "Silver": "risk_off",
    "WTI Oil": "risk_off",
    "Brent Oil": "risk_off",
    "USD/JPY": "risk_off",
    # Risk-on: indici azionari + crypto
    "US500": "risk_on",
    "Nasdaq 100": "risk_on",
    "DAX 40": "risk_on",
    "Bitcoin": "risk_on",
    "Ethereum": "risk_on",
    "Solana": "risk_on",
    "Ripple": "risk_on",
    "Cardano": "risk_on",
    "Avalanche": "risk_on",
    "Polkadot": "risk_on",
    "Chainlink": "risk_on",
    "Dogecoin": "risk_on",
    # Neutro: forex major
    "EUR/USD": "neutro",
    "GBP/USD": "neutro",
}


# Mapping paese Finnhub -> asset che tipicamente reagiscono. Usato per
# l'economic_calendar (FOMC, CPI, NFP, ecc) dove non c'e'
# impact_assets esplicito.
COUNTRY_TO_ASSETS: dict[str, set[str]] = {
    "US": {"US500", "Nasdaq 100", "Gold", "Silver", "WTI Oil", "Brent Oil", "USD/JPY"},
    "EU": {"DAX 40", "EUR/USD"},
    "DE": {"DAX 40", "EUR/USD"},
    "GB": {"GBP/USD"},
    "JP": {"USD/JPY"},
}


def macro_group_of(asset_name: str) -> str:
    """Ritorna il macro_group per l'asset, 'other' se non mappato."""
    return MACRO_GROUPS.get(asset_name, "other")


def _event_affects_asset(
    asset_name: str, event: dict[str, Any], source: str
) -> bool:
    """True se l'evento riguarda l'asset. ``source`` e' 'critical' o
    'calendar' per distinguere il formato del record."""
    if source == "critical":
        return asset_name in (event.get("impact_assets") or [])
    # economic_calendar: match su country
    country = (event.get("country") or "").upper()
    return asset_name in COUNTRY_TO_ASSETS.get(country, set())


@dataclass
class GuardrailLog:
    asset: str
    action: str  # 'dropped_binary' | 'score_penalty_opposite' | 'stop_widened' | 'score_penalty_correlation'
    details: str


def events_affecting_asset(
    asset_name: str,
    critical_events: list[dict[str, Any]],
    economic_calendar: list[dict[str, Any]],
    within_hours: float = 24.0,
) -> list[dict[str, Any]]:
    """Ritorna gli eventi che toccano l'asset entro ``within_hours``,
    normalizzati con un campo 'source' ('critical' | 'calendar') e
    'hours_until' gia' presente nei dati originali.
    """
    out: list[dict[str, Any]] = []
    for ev in critical_events or []:
        if ev.get("hours_until", 999) <= within_hours and _event_affects_asset(
            asset_name, ev, "critical"
        ):
            out.append({**ev, "source": "critical"})
    for ev in economic_calendar or []:
        if ev.get("hours_until", 999) <= within_hours and _event_affects_asset(
            asset_name, ev, "calendar"
        ):
            out.append({**ev, "source": "calendar"})
    out.sort(key=lambda e: e.get("hours_until", 999))
    return out


def _is_opposite_direction(direction: str, direction_hint: str) -> bool:
    """True se la direzione proposta va contro il direction_hint
    (es. LONG su asset con hint 'risk_off' o 'risk_off_if_fails')."""
    d = (direction or "").lower()
    h = (direction_hint or "").lower()
    if d == "long" and h in ("risk_off", "risk_off_if_fails"):
        return True
    if d == "short" and h in ("risk_on", "risk_on_if_fails"):
        return True
    return False


def apply_macro_guardrails(
    proposals: list[SetupProposal],
    critical_events: list[dict[str, Any]],
    economic_calendar: list[dict[str, Any]],
    open_position_assets: list[str] | None = None,
) -> tuple[list[SetupProposal], list[GuardrailLog]]:
    """Applica le regole deterministiche ai proposal. Ritorna
    (new_proposals, log). Chi chiama deve loggare ``log`` per
    visibilita'."""
    open_position_assets = open_position_assets or []
    open_groups = {
        macro_group_of(a) for a in open_position_assets if a
    } - {"other", "neutro"}

    out_proposals: list[SetupProposal] = []
    logs: list[GuardrailLog] = []

    for p in proposals:
        # Copia modificabile del proposal (SetupProposal e' frozen? no, @dataclass plain).
        new_score = p.score
        new_stop = p.suggested_stop_pct
        drop = False
        drop_reason = ""

        relevant = events_affecting_asset(
            p.asset, critical_events, economic_calendar, within_hours=24.0
        )

        # Regole su critical_events (solo quelli con direction_hint noto)
        binary_event_near = None
        for ev in relevant:
            if ev.get("source") != "critical":
                continue
            hint = (ev.get("direction_hint") or "").lower()
            if hint in ("risk_off_if_fails", "risk_on_if_fails"):
                binary_event_near = ev
                # Regola hard: score < 8 per un evento binario imminente -> drop
                if p.score < 8.0:
                    drop = True
                    drop_reason = (
                        f"evento binario {ev.get('description','?')} "
                        f"tra {ev.get('hours_until','?')}h (serve score >= 8)"
                    )
                    break
            # Regola: direction opposta a direction_hint -> penalita' -2
            if _is_opposite_direction(p.direction, hint):
                new_score -= 2.0
                logs.append(GuardrailLog(
                    asset=p.asset,
                    action="score_penalty_opposite",
                    details=(
                        f"-2.0 per direzione {p.direction.upper()} "
                        f"opposta a {hint} ({ev.get('description','?')})"
                    ),
                ))

        if drop:
            logs.append(GuardrailLog(
                asset=p.asset,
                action="dropped_binary",
                details=f"score {p.score:.1f}: {drop_reason}",
            ))
            continue

        # Regola: un qualsiasi evento high-impact entro 24h -> stop *1.5
        if relevant and new_stop and new_stop > 0:
            widened = new_stop * 1.5
            if widened > new_stop:
                logs.append(GuardrailLog(
                    asset=p.asset,
                    action="stop_widened",
                    details=(
                        f"stop_pct {new_stop:.2f} -> {widened:.2f} "
                        f"({len(relevant)} eventi entro 24h)"
                    ),
                ))
                new_stop = widened

        # Regola: correlazione con posizioni aperte
        group = macro_group_of(p.asset)
        if group in open_groups and group != "other":
            new_score -= 1.5
            logs.append(GuardrailLog(
                asset=p.asset,
                action="score_penalty_correlation",
                details=(
                    f"-1.5: gia' esposto {group} "
                    f"(posizioni aperte: {', '.join(open_position_assets)})"
                ),
            ))

        # Applica le modifiche al proposal. SetupProposal e' mutabile
        # (dataclass senza frozen=True).
        p.score = round(new_score, 2)
        p.suggested_stop_pct = round(new_stop, 4) if new_stop else new_stop
        out_proposals.append(p)

    return out_proposals, logs
