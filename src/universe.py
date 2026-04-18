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
    Asset("DAX 40", "DE40", "index"),
    Asset("EUR/USD", "EURUSD", "fx"),
    Asset("GBP/USD", "GBPUSD", "fx"),
    Asset("USD/JPY", "USDJPY", "fx"),
    # Crypto: peso ridotto in settimana, priorita' nel weekend quando i
    # mercati tradizionali sono chiusi. La regola e' codificata nel
    # system prompt del LLM, qui le includiamo solo nell'universo.
    Asset("Bitcoin", "BTCUSD", "crypto", notes="weekend-friendly"),
    Asset("Ethereum", "ETHUSD", "crypto", notes="weekend-friendly"),
    Asset("Solana", "SOLUSD", "crypto", notes="weekend-friendly"),
    Asset("Ripple", "XRPUSD", "crypto", notes="weekend-friendly"),
    Asset("Cardano", "ADAUSD", "crypto", notes="weekend-friendly"),
    Asset("Avalanche", "AVAXUSD", "crypto", notes="weekend-friendly"),
    Asset("Polkadot", "DOTUSD", "crypto", notes="weekend-friendly"),
    Asset("Chainlink", "LINKUSD", "crypto", notes="weekend-friendly"),
    Asset("Dogecoin", "DOGEUSD", "crypto", notes="weekend-friendly"),
]
