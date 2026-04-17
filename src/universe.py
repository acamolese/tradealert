"""Universo iniziale di asset Capital.com per la Fase 1 (Coach).

Gli epic sono identificativi Capital.com. Quelli qui sono stime ragionevoli
ma vanno verificati al primo run reale tramite client.search_market(...).
Se un epic risulta sbagliato, il job lo loggera' e si saltera' senza bloccare
il resto.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    name: str
    epic: str
    asset_class: str  # "metal" | "energy" | "index" | "fx"
    notes: str = ""


UNIVERSE: list[Asset] = [
    Asset("Gold", "GOLD", "metal"),
    Asset("Silver", "SILVER", "metal"),
    Asset("WTI Oil", "OIL_CRUDE", "energy"),
    Asset("Brent Oil", "OIL_BRENT", "energy"),
    Asset("US500", "US500", "index"),
    Asset("Nasdaq 100", "US100", "index"),
    Asset("DAX 40", "GERMANY40", "index"),
    Asset("EUR/USD", "EURUSD", "fx"),
    Asset("GBP/USD", "GBPUSD", "fx"),
    Asset("USD/JPY", "USDJPY", "fx"),
]
