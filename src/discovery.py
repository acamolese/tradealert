"""Discovery dinamica dei top mover del momento.

Usa l'endpoint ``/marketnavigation`` di Capital.com che restituisce
direttamente ``percentageChange``, ``bid/offer`` e ``marketStatus`` per
tutti i mercati di una categoria (crypto, shares US popolari, ecc.)
in una sola chiamata. Rispetto al vecchio approccio basato su una
watchlist hardcoded, copre dinamicamente ogni asset listato da Capital
senza doverlo mantenere manualmente.

Fallback: se la chiamata fallisce per qualunque motivo, la discovery
torna vuota e lo scanner procede solo sull'``UNIVERSE`` statico.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .capital_client import CapitalAPIError, CapitalClient
from .universe import Asset

log = logging.getLogger(__name__)


@dataclass
class MoverQuote:
    asset: Asset
    percentage_change: float
    last_price: float | None
    high: float | None
    low: float | None


_INSTRUMENT_TYPE_TO_CLASS = {
    "CRYPTOCURRENCIES": "crypto",
    "SHARES": "share",
    "CURRENCIES": "fx",
    "INDICES": "index",
    "COMMODITIES": "commodity",
}

# Nodi Capital che vogliamo interrogare. Crypto: l'intero gruppo
# (~265 mercati) cosi' scopriamo anche alt mid/small cap. Shares:
# popular_shares copre ~400 azioni US/EU liquide, sufficiente per
# momentum intraday. most_volatile cattura small cap con grossa
# percentage change che popular potrebbe escludere.
DEFAULT_CRYPTO_NODE = "hierarchy_v1.crypto_currencies"
DEFAULT_SHARE_NODES = (
    "hierarchy_v1.shares.popular_shares",
    "hierarchy_v1.shares.us.most_volatile",
)


def _market_to_quote(market: dict[str, Any]) -> MoverQuote | None:
    epic = market.get("epic")
    pct = market.get("percentageChange")
    instrument_type = market.get("instrumentType") or ""
    if not epic or pct is None:
        return None
    asset_class = _INSTRUMENT_TYPE_TO_CLASS.get(instrument_type)
    if not asset_class:
        return None
    bid = market.get("bid")
    offer = market.get("offer")
    last = (
        (float(bid) + float(offer)) / 2
        if bid is not None and offer is not None
        else None
    )
    return MoverQuote(
        asset=Asset(
            name=market.get("instrumentName") or epic,
            epic=epic,
            asset_class=asset_class,
        ),
        percentage_change=float(pct),
        last_price=last,
        high=market.get("high"),
        low=market.get("low"),
    )


def _fetch_node_markets(
    capital: CapitalClient, node_id: str
) -> list[dict[str, Any]]:
    try:
        data = capital.get_market_navigation(node_id)
    except CapitalAPIError as exc:
        log.warning("Navigation skip %s: %s", node_id, exc)
        return []
    except Exception as exc:
        log.warning("Navigation errore %s: %s", node_id, exc)
        return []
    return data.get("markets") or []


def _default_nodes(now: datetime | None = None) -> list[str]:
    """Nel weekend i nodi share sono inutili (mercati chiusi): saltiamo
    per risparmiare chiamate HTTP e tempo."""
    now = now or datetime.now(ZoneInfo("Europe/Rome"))
    is_weekend = now.weekday() >= 5
    nodes = [DEFAULT_CRYPTO_NODE]
    if not is_weekend:
        nodes.extend(DEFAULT_SHARE_NODES)
    return nodes


def discover_top_movers(
    capital: CapitalClient,
    watchlist: list[Asset] | None = None,  # retro-compat, ignorato
    top_n: int = 5,
    abs_min_pct: float = 2.0,
    nodes: list[str] | None = None,
) -> tuple[list[Asset], list[MoverQuote]]:
    """Ritorna (lista_asset, quote_details) con i top mover del momento
    letti dai nodi navigazione di Capital.

    Gli asset sono ordinati: prima i gainer (discending per pct), poi i
    loser (ascending). Epic duplicati vengono rimossi preservando
    l'ordine. Filtro: solo mercati TRADEABLE con |pct| >= abs_min_pct.

    ``watchlist`` e' accettato per retro-compatibilita' ma ignorato:
    la watchlist statica non e' piu' usata, la scoperta e' interamente
    basata sui nodi Capital. ``nodes`` permette di sovrascrivere i
    default (crypto + share popular) per test o casi speciali.
    """
    nodes_to_fetch = nodes or _default_nodes()
    all_quotes: dict[str, MoverQuote] = {}
    total_markets = 0
    for node_id in nodes_to_fetch:
        markets = _fetch_node_markets(capital, node_id)
        total_markets += len(markets)
        for m in markets:
            if m.get("marketStatus") != "TRADEABLE":
                continue
            q = _market_to_quote(m)
            if q is None:
                continue
            # in caso di sovrapposizione fra nodi, tieni la quota con pct
            # assoluto maggiore (piu' probabilmente rilevante)
            existing = all_quotes.get(q.asset.epic)
            if (
                existing is None
                or abs(q.percentage_change) > abs(existing.percentage_change)
            ):
                all_quotes[q.asset.epic] = q

    if not all_quotes:
        log.warning(
            "Discovery: zero quote valide dai nodi %s (totale mercati letti: %d)",
            nodes_to_fetch,
            total_markets,
        )
        return [], []

    filtered = [
        q for q in all_quotes.values()
        if abs(q.percentage_change) >= abs_min_pct
    ]

    gainers = sorted(
        filtered, key=lambda q: q.percentage_change, reverse=True
    )[:top_n]
    losers = sorted(filtered, key=lambda q: q.percentage_change)[:top_n]

    selected: list[Asset] = []
    seen: set[str] = set()
    for q in gainers + losers:
        if q.asset.epic not in seen:
            selected.append(q.asset)
            seen.add(q.asset.epic)

    log.info(
        "Discovery nav: nodi=%s, %d mercati totali, %d TRADEABLE, "
        "%d sopra soglia %.1f%%, %d gainers, %d losers, %d selezionati",
        nodes_to_fetch,
        total_markets,
        len(all_quotes),
        len(filtered),
        abs_min_pct,
        len(gainers),
        len(losers),
        len(selected),
    )

    details = list(gainers) + list(losers)
    return selected, details
