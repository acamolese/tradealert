"""Universo iniziale di asset Capital.com per la Fase 1 (Coach).

Sprint 1 (Fix 1.3): universo ridotto a 5 asset core finché non viene
accumulato un sample size di 30-50 trade chiusi su questo set per
validare l'edge. Settori coperti: indici US (rischio risk-on),
metallo prezioso (safe haven), energia (commodity ciclica), crypto major
(unico asset H24, gestione weekend).

Gli epic sono identificativi Capital.com. Se un epic risulta sbagliato,
il job lo loggera' e si saltera' senza bloccare il resto.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    name: str
    epic: str
    asset_class: str  # "metal" | "energy" | "index" | "fx" | "crypto"
    notes: str = ""


UNIVERSE: list[Asset] = [
    Asset("Gold", "GOLD", "metal"),
    Asset("Brent Oil", "OIL_BRENT", "energy"),
    Asset("US500", "US500", "index"),
    Asset("Nasdaq 100", "US100", "index"),
    Asset("Bitcoin", "BTCUSD", "crypto", notes="weekend-friendly"),
]
