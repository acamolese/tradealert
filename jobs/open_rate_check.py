"""Monitor del tasso di apertura post-deploy pipeline a due chiamate (Sprint 4 t1).

Validazione sul campo del flag SCORING_TWO_CALL: confronta il tasso di scan
"produttivi" (signal_sent + slots_full) DOPO l'attivazione con il baseline
pre-deploy. Se cala sotto soglia con campione sufficiente, avvisa su Telegram
(la pipeline temp 0.2 starebbe aprendo meno -> valutare SCORING_TWO_CALL=false).

Silenzioso quando va bene (solo log): nessuno spam quotidiano.

Cron (TZ Europe/Rome), dopo l'ultimo scan:
    35 22 * * *  cd $TRADEALERT_HOME && python -m jobs.open_rate_check >> logs/open_rate.log 2>&1
"""

from __future__ import annotations

import logging
import sys
from collections import Counter

from src.config import load_config
from src.db import Database
from src.telegram_client import TelegramClient

log = logging.getLogger(__name__)

# Inizio raccolta post-deploy: istante (circa) di attivazione SCORING_TWO_CALL.
DEPLOY_START = "2026-06-09T16:00:00+00:00"
BASELINE_RATE = 9.6   # % scan produttivi pre-deploy (signal_sent + slots_full)
BASELINE_PER_DAY = 1.47
ALERT_RATE = 5.0      # soglia di allarme sul tasso produttivo
MIN_SCANS = 40        # campione minimo (~2.5 giorni) prima di poter allarmare


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    db = Database(config)

    runs = (
        db._client.table("scanner_runs")
        .select("outcome,ran_at")
        .gte("ran_at", DEPLOY_START)
        .execute()
        .data
    )
    sigs = (
        db._client.table("signals")
        .select("id,created_at")
        .gte("created_at", DEPLOY_START)
        .execute()
        .data
    )
    n = len(runs)
    oc = Counter(r["outcome"] for r in runs)
    productive = oc.get("signal_sent", 0) + oc.get("slots_full", 0)
    rate = (100.0 * productive / n) if n else 0.0
    days = len({(r["ran_at"] or "")[:10] for r in runs}) or 1
    per_day = len(sigs) / days

    log.info(
        "open-rate post-deploy: %.1f%% (%d/%d scan produttivi, %.2f signal/gg, %d giorni) "
        "| baseline %.1f%% / %.2f gg",
        rate, productive, n, per_day, days, BASELINE_RATE, BASELINE_PER_DAY,
    )

    if n < MIN_SCANS:
        log.info("Campione post-deploy insufficiente (%d/%d scan). Niente allarme.", n, MIN_SCANS)
        return 0

    if rate < ALERT_RATE:
        msg = (
            f"⚠️ <b>Tasso di apertura in calo post-deploy due-chiamate</b>\n"
            f"Post-deploy: <b>{rate:.1f}%</b> scan produttivi ({productive}/{n}), "
            f"{per_day:.2f} signal/gg su {days} giorni.\n"
            f"Baseline pre-deploy: {BASELINE_RATE:.1f}% / {BASELINE_PER_DAY:.2f} gg.\n"
            f"La pipeline temp 0.2 potrebbe aprire MENO trade. Valuta il rollback: "
            f"<code>SCORING_TWO_CALL=false</code> nel .env della VM (niente redeploy)."
        )
        try:
            TelegramClient(config).send_message(msg)
            log.info("Allarme open-rate inviato su Telegram.")
        except Exception:
            log.exception("Invio allarme Telegram fallito")
    else:
        log.info("Tasso di apertura nella norma (%.1f%% >= %.1f%%). Nessun allarme.", rate, ALERT_RATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
