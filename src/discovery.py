"""Discovery dinamica dei top mover del momento.

Dato una watchlist ampia, chiama ``capital.get_market`` per ogni epic e
legge ``snapshot.percentageChange`` (change % della sessione corrente).
Ritorna top N gainers + top N losers sopra una soglia minima, filtrando
fuori gli asset con mercato chiuso. Tollerante a 404 e rate limit: un
asset non accessibile viene loggato e saltato senza bloccare il resto.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

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


def discover_top_movers(
    capital: CapitalClient,
    watchlist: list[Asset],
    top_n: int = 5,
    abs_min_pct: float = 2.0,
    request_delay_sec: float = 0.15,
) -> tuple[list[Asset], list[MoverQuote]]:
    """Ritorna (lista_asset, quote_details).

    ``lista_asset`` e' pronta per l'unione con ``UNIVERSE``, nell'ordine
    gainer prima (discending per pct), poi loser (ascending). Duplicati
    sono rimossi preservando l'ordine.
    """
    quotes: list[MoverQuote] = []

    for asset in watchlist:
        try:
            market = capital.get_market(asset.epic)
        except CapitalAPIError as exc:
            log.info(
                "Discovery skip %s (%s): %s", asset.name, asset.epic, exc
            )
            time.sleep(request_delay_sec)
            continue
        except Exception as exc:
            log.warning("Discovery errore %s: %s", asset.name, exc)
            time.sleep(request_delay_sec)
            continue

        snap = market.get("snapshot", {}) or {}
        if snap.get("marketStatus") != "TRADEABLE":
            time.sleep(request_delay_sec)
            continue

        pct = snap.get("percentageChange")
        if pct is None:
            time.sleep(request_delay_sec)
            continue

        bid = snap.get("bid")
        offer = snap.get("offer")
        last = (
            (float(bid) + float(offer)) / 2
            if bid is not None and offer is not None
            else None
        )

        quotes.append(
            MoverQuote(
                asset=asset,
                percentage_change=float(pct),
                last_price=last,
                high=snap.get("high"),
                low=snap.get("low"),
            )
        )
        time.sleep(request_delay_sec)

    if not quotes:
        log.warning("Discovery: zero quote valide dalla watchlist (%d asset)", len(watchlist))
        return [], []

    filtered = [q for q in quotes if abs(q.percentage_change) >= abs_min_pct]

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
        "Discovery su %d asset: %d quote valide, %d sopra soglia %.1f%%, "
        "%d gainers, %d losers, %d selezionati",
        len(watchlist),
        len(quotes),
        len(filtered),
        abs_min_pct,
        len(gainers),
        len(losers),
        len(selected),
    )

    # Ritorno anche i details per logging/Telegram, ordine = gainers + losers
    details = [q for q in gainers + losers]
    return selected, details
