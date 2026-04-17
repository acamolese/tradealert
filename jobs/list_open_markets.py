"""Diagnostico: stampa lo stato di apertura di ogni asset dell'universo,
piu' una lista di candidati "weekend" comuni di Capital.com da provare.

Uso:
    python -m jobs.list_open_markets

Utile per capire se in questo momento c'e' qualcosa di tradeable per i test.
"""

from __future__ import annotations

import logging
import sys

from src.capital_client import CapitalAPIError, CapitalClient
from src.config import load_config
from src.universe import UNIVERSE

WEEKEND_CANDIDATES = [
    # Capital.com offre alcuni CFD weekend; i nomi epic possono variare.
    # Provo qualche combinazione plausibile, errori 404/closed sono normali.
    "WEEKEND_OIL_CRUDE",
    "WEEKEND_OIL",
    "OIL_CRUDE_WEEKEND",
    "WEEKEND_US500",
    "US500_WEEKEND",
    "WEEKEND_GOLD",
    "GOLD_WEEKEND",
    "WEEKEND_GERMANY40",
    "GERMANY40_WEEKEND",
    # Crypto principali (sempre aperti)
    "BTCUSD",
    "ETHUSD",
]


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    config = load_config()
    capital = CapitalClient(config)
    capital.login()

    print(f"Ambiente: {config.capital_env}")
    print()
    print(f"{'Asset':<25} {'Epic':<25} {'Status':<15} {'Bid':<12}")
    print("-" * 80)

    for asset in UNIVERSE:
        _check_one(capital, asset.name, asset.epic)

    print()
    print("--- Candidati weekend / sempre-on ---")
    for epic in WEEKEND_CANDIDATES:
        _check_one(capital, epic, epic)

    return 0


def _check_one(capital: CapitalClient, label: str, epic: str) -> None:
    try:
        market = capital.get_market(epic)
    except CapitalAPIError as exc:
        if exc.status == 404:
            print(f"{label:<25} {epic:<25} {'NOT_FOUND':<15} -")
        else:
            print(f"{label:<25} {epic:<25} {'ERR ' + str(exc.status):<15} -")
        return
    snap = market.get("snapshot", {})
    status = snap.get("marketStatus", "UNKNOWN")
    bid = snap.get("bid", "-")
    print(f"{label:<25} {epic:<25} {status:<15} {bid}")


if __name__ == "__main__":
    sys.exit(main())
