"""Conferma manuale di un signal pendente in modalita 'confirm'.

Uso:
    python -m jobs.confirm <signal_id>

Carica il signal dal DB, verifica che sia ancora 'pending', recupera le
feature di mercato correnti per l'asset, esegue il signal su Capital.com
con tutti i safety dell'executor, notifica esito su Telegram.
"""

from __future__ import annotations

import logging
import sys

from src.capital_client import CapitalClient
from src.config import load_config
from src.db import Database
from src.executor import execute_signal
from src.features import compute_features
from src.scanner import _format_execution_message  # riuso il formatter
from src.telegram_client import TelegramClient
from src.universe import UNIVERSE


def _resolve_epic(asset_name: str) -> str | None:
    for asset in UNIVERSE:
        if asset.name == asset_name:
            return asset.epic
    return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("Uso: python -m jobs.confirm <signal_id>")
        return 2
    signal_id = int(sys.argv[1])

    config = load_config()
    db = Database(config)
    telegram = TelegramClient(config)

    signal_row = db.get_signal(signal_id)
    if not signal_row:
        print(f"Signal {signal_id} non trovato")
        return 1
    if signal_row["status"] != "pending":
        print(
            f"Signal {signal_id} non e' pending (stato attuale: {signal_row['status']})"
        )
        return 1

    epic = _resolve_epic(signal_row["asset"])
    if not epic:
        print(f"Asset {signal_row['asset']} non e' nell'universo configurato")
        return 1

    capital = CapitalClient(config)
    capital.login()
    candles = capital.get_prices(epic, resolution="HOUR_4", max_bars=60)
    snapshot = capital.get_market(epic)
    asset_features = compute_features(
        signal_row["asset"], candles, snapshot=snapshot
    )
    asset_features["epic"] = epic

    result = execute_signal(config, capital, db, signal_row, asset_features)

    # Riuso il formatter: serve un oggetto con .asset e .direction
    class _P:
        asset = signal_row["asset"]
        direction = signal_row["direction"]

    telegram.send_message(_format_execution_message(result, _P()))
    print(
        "Esecuzione" + (" RIUSCITA" if result.executed else f" SALTATA: {result.reason}")
    )
    return 0 if result.executed else 1


if __name__ == "__main__":
    sys.exit(main())
