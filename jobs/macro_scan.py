"""Scan giornaliero eventi macro binari dalle news.

Cron: 06:50 Europe/Rome (prima del primo scanner delle 07:05).

Cosa fa:
1. Fetcha news recenti (RSS + Finnhub se configurato).
2. Passa le news a Claude Haiku per estrarre eventi binari nelle prossime 72h.
3. Rimpiazza gli eventi con source='auto' nel file critical_events.json
   preservando intatti quelli con source='manual' aggiunti via /evento.
4. Logga conteggio aggiunti/eliminati.

Non invia Telegram: il report degli eventi si vede con /eventi.
"""

from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.event_extractor import extract_events
from src.market_context import (
    list_all_events,
    prune_past_events,
    replace_auto_events,
)
from src.news import fetch_news


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("macro_scan")

    config = load_config()

    pruned = prune_past_events()
    if pruned:
        log.info("Rimossi %d eventi passati", pruned)

    news = fetch_news(config, limit=40)
    log.info("News recuperate: %d", len(news))
    if not news:
        log.info("Nessuna news disponibile, skip estrazione")
        return 0

    new_auto = extract_events(config, news, hours_ahead=72)
    log.info("Eventi auto estratti: %d", len(new_auto))

    # Conteggio pre-scrittura per diff nel log
    before = [
        e for e in list_all_events() if e.get("source") == "auto"
    ]
    replace_auto_events(new_auto)
    log.info(
        "Eventi auto aggiornati: prima=%d, dopo=%d",
        len(before),
        len(new_auto),
    )

    for ev in new_auto:
        log.info(
            "  + %s | %s | %s | %s",
            ev["date"],
            ev["description"],
            ",".join(ev["impact_assets"]),
            ev["direction_hint"],
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
