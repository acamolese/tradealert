"""Entry point del briefing 3x/giorno.

Uso:
    python -m jobs.briefing morning
    python -m jobs.briefing afternoon
    python -m jobs.briefing evening

Lo slot viene passato dall'environment BRIEFING_SLOT nel workflow Actions,
oppure come argv[1] in locale.
"""

from __future__ import annotations

import logging
import os
import sys

# Kill switch: disabilitato di default dal 2026-04-24 per taglio costi API.
# Per riabilitare impostare BRIEFING_ENABLED=true nell'ambiente (.env sulla VM).
if os.environ.get("BRIEFING_ENABLED", "false").lower() != "true":
    print("[briefing] disabled via BRIEFING_ENABLED env var")
    sys.exit(0)

from src.briefing import run_briefing
from src.config import load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    slot = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("BRIEFING_SLOT", "morning")
    ).lower()
    if slot not in ("morning", "afternoon", "evening"):
        print(f"Slot non riconosciuto: {slot}")
        return 2
    config = load_config()
    run_briefing(config, slot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
