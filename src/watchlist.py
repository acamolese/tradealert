"""Watchlist estesa per la discovery dinamica.

Non sostituisce ``UNIVERSE`` (gli asset sempre scansionati) ma fornisce
al modulo ``src.discovery`` il bacino da cui pescare top movers del
momento da iniettare nello scan. Epic confermati tradeable su Capital,
ma discovery e' comunque tollerante a 404/rate limit (skip silenzioso).
"""

from __future__ import annotations

from .universe import Asset

DISCOVERY_WATCHLIST: list[Asset] = [
    # --- Crypto (weekend-friendly, sempre aperte) ---
    Asset("Bitcoin", "BTCUSD", "crypto"),
    Asset("Ethereum", "ETHUSD", "crypto"),
    Asset("Solana", "SOLUSD", "crypto"),
    Asset("Ripple", "XRPUSD", "crypto"),
    Asset("Cardano", "ADAUSD", "crypto"),
    Asset("Avalanche", "AVAXUSD", "crypto"),
    Asset("Polkadot", "DOTUSD", "crypto"),
    Asset("Chainlink", "LINKUSD", "crypto"),
    Asset("Dogecoin", "DOGEUSD", "crypto"),
    Asset("Ethereum Classic", "ETCUSD", "crypto"),
    Asset("EthereumFi", "ETHFIUSD", "crypto"),
    Asset("EthereumPoW", "ETHWUSD", "crypto"),
    Asset("ARPA", "ARPAUSD", "crypto"),

    # --- Azioni US large cap (tradeable 15:30-22:00 IT, lun-ven) ---
    Asset("Apple", "AAPL", "share"),
    Asset("Microsoft", "MSFT", "share"),
    Asset("Nvidia", "NVDA", "share"),
    Asset("Tesla", "TSLA", "share"),
    Asset("Alphabet", "GOOGL", "share"),
    Asset("Amazon", "AMZN", "share"),
    Asset("Meta", "META", "share"),
    Asset("AMD", "AMD", "share"),
    Asset("Netflix", "NFLX", "share"),
    Asset("JPMorgan", "JPM", "share"),
    Asset("Coinbase", "COIN", "share"),
    Asset("Palantir", "PLTR", "share"),
    Asset("Super Micro", "SMCI", "share"),
    Asset("Dave & Buster's", "PLAY", "share"),

    # --- Indici globali ---
    Asset("US500", "US500", "index"),
    Asset("Nasdaq 100", "US100", "index"),
    Asset("DAX 40", "DE40", "index"),
    Asset("FTSE 100", "UK100", "index"),
    Asset("Nikkei 225", "JP225", "index"),

    # --- Commodities ---
    Asset("Gold", "GOLD", "metal"),
    Asset("Silver", "SILVER", "metal"),
    Asset("WTI Oil", "OIL_CRUDE", "energy"),
    Asset("Brent Oil", "OIL_BRENT", "energy"),
    Asset("Natural Gas", "NATURALGAS", "energy"),

    # --- Forex majors ---
    Asset("EUR/USD", "EURUSD", "fx"),
    Asset("GBP/USD", "GBPUSD", "fx"),
    Asset("USD/JPY", "USDJPY", "fx"),
    Asset("AUD/USD", "AUDUSD", "fx"),
    Asset("USD/CHF", "USDCHF", "fx"),
]
