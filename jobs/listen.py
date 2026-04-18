"""Entry point del listener Telegram.

Modalita':
- Default (nessun arg, LISTEN_SECONDS assente o <=0): daemon infinito
  tramite ``listen_forever``. E' la modalita' usata da systemd sulla VM.
- Con arg numerico o LISTEN_SECONDS > 0: one-shot ``listen_once`` per N
  secondi, per test manuali o invocazioni cron legacy.

Uso:
    python -m jobs.listen          # daemon
    python -m jobs.listen 60       # ascolta 60s e esce
"""

from __future__ import annotations

import logging
import os
import sys

from src.command_listener import listen_forever, listen_once
from src.config import load_config


def _parse_seconds() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LISTEN_SECONDS", "0")
    try:
        return int(arg)
    except (TypeError, ValueError):
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    seconds = _parse_seconds()

    if seconds > 0:
        cmd = listen_once(config, max_seconds=seconds)
        if cmd:
            print(f"Comando processato: {cmd}")
        else:
            print("Nessun comando ricevuto entro il timeout")
        return 0

    listen_forever(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
