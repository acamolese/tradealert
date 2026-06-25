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

# Sprint 5 — primo blocco FX (diversificatori veri: driver valutari/tassi,
# scorrelati da commodity/equity/crypto). Tutti quote=USD -> sizing corretto
# anche con SIZING_CURRENCY_AWARE OFF (fattore 1.0). Spread in sessione ottimi
# (EUR/USD 0.006%, AUD/USD 0.009%, GBP/USD 0.010%, piu' stretti di Brent/Gold).
# Appesi all'universo SOLO se BASKET_FX_ENABLED. USD/JPY escluso (sizing
# non-currency-aware, vedi docs/sprint5-sizing-fix.md). Vedi
# docs/sprint5-basket-concentration.md.
FX_BLOCK_1: list[Asset] = [
    Asset("EUR/USD", "EURUSD", "fx"),
    Asset("AUD/USD", "AUDUSD", "fx"),
    Asset("GBP/USD", "GBPUSD", "fx"),
]

# Sprint 5 — blocco "trending diversifiers" (al posto degli FX, che non hanno
# mai prodotto setup >=7: range-bound). Criterio: trenda + volatile (ATR% nel
# range produttivo, non FX-basso) + driver diverso dal trio Brent/Nasdaq/Gold.
# Copper: metalli industriali/Cina (USD). Hang Seng: equity Cina/HK (HKD).
# Nikkei: equity Giappone (JPY). HKD/JPY richiedono SIZING_CURRENCY_AWARE=ON.
# Appesi all'universo SOLO se BASKET_TREND_ENABLED. Vedi
# docs/sprint5-basket-concentration.md.
TREND_BLOCK_1: list[Asset] = [
    Asset("Copper", "COPPER", "metal"),
    Asset("Hang Seng", "HK50", "index"),
    Asset("Nikkei", "J225", "index"),
]

# Tutti gli asset CONOSCIUTI dal sistema (universo base + blocchi opzionali).
# Usato per risolvere epic e classe (monitor, intra_trade, concentration_shadow)
# a PRESCINDERE da quali blocchi sono abilitati per lo scan: una posizione su un
# asset di un blocco deve essere risolvibile anche se lo scan di quel blocco e'
# poi spento. Lo scanning resta gated nei flag.
ALL_KNOWN: list[Asset] = UNIVERSE + FX_BLOCK_1 + TREND_BLOCK_1
