"""Entry point: ascolta comandi Telegram (es. /posizioni) per N secondi
e processa il primo riconosciuto.

Uso locale:
    python -m jobs.listen [secondi]

Se chiamato senza argomenti, ascolta per LISTEN_SECONDS dal config (default 60).
"""

from __future__ import annotations

import logging
import os
import sys

from src.command_listener import listen_once
from src.config import load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    seconds = int(
        sys.argv[1]
        if len(sys.argv) > 1 and sys.argv[1].isdigit()
        else os.environ.get("LISTEN_SECONDS", "60")
    )
    config = load_config()
    cmd = listen_once(config, max_seconds=seconds)
    if cmd:
        print(f"Comando processato: {cmd}")
    else:
        print("Nessun comando ricevuto entro il timeout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
