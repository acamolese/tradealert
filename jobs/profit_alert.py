"""Avviso di profitto v2 — SUGGERISCE, non esegue.

Quando il guadagno flottante della posizione aperta supera soglie significative
(default +15/+25/+40% del conto), manda un avviso Telegram che ti invita a valutare
se consolidare, spiegando il trade-off (incassare = uscire dal mercato; il
controller potrebbe riaprire). NON chiude nulla: la decisione e' sempre tua.

Coerente col test sprint9 (il trailing AUTOMATICO e' dannoso): qui l'avviso e' raro,
a soglie di guadagno, e l'azione e' discrezionale.

Config: V2_PROFIT_ALERT_PCTS="15,25,40" (regolabile). Dedup una volta per soglia.
Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.profit_alert [--dry-run]
"""
from __future__ import annotations

import logging
import os
import sys

from src.config import load_config
from src.exposure_config import load_exposure_config

log = logging.getLogger(__name__)


def _thresholds() -> list[float]:
    raw = os.environ.get("V2_PROFIT_ALERT_PCTS", "15,25,40")
    try:
        return sorted(float(x) for x in raw.split(",") if x.strip())
    except ValueError:
        return [15.0, 25.0, 40.0]


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv

    from src.capital_client import CapitalClient
    from src.db import Database
    from src.telegram_client import TelegramClient

    v1 = load_config()
    cfg = load_exposure_config()
    db = Database(v1)
    capital = CapitalClient(v1)
    capital.login()

    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    balance = float(bal.get("balance") or 0.0)     # realizzato
    floating = float(bal.get("profitLoss") or 0.0)  # flottante (upl posizioni)
    if balance <= 0:
        log.info("balance non leggibile, skip.")
        return 0
    gain_pct = floating / balance * 100.0
    thr = _thresholds()

    # ultima soglia gia' avvisata (0 se reset)
    last = 0.0
    try:
        ev = (db._client.table("monitoring_events").select("details,created_at")
              .eq("event_type", "profit_alert").order("created_at", desc=True)
              .limit(1).execute().data)
        if ev:
            last = float((ev[0].get("details") or {}).get("threshold") or 0.0)
    except Exception:
        log.exception("lettura profit_alert fallita (proseguo)")

    crossed = max([s for s in thr if gain_pct >= s], default=0.0)
    print(f"floating {floating:+.2f}€ ({gain_pct:+.1f}% del conto) | soglie {thr} | "
          f"superata {crossed or '-'} | gia' avvisata {last or '-'}")

    # reset: se il guadagno e' sceso sotto la soglia minima, azzera lo stato
    if gain_pct < thr[0] and last > 0 and not dry:
        db.insert_monitoring_event({
            "event_type": "profit_alert", "reason": "reset (sotto soglia minima)",
            "details": {"threshold": 0.0, "gain_pct": round(gain_pct, 2)},
        })
        log.info("profit_alert reset (gain sotto %.0f%%)", thr[0])
        return 0

    if crossed <= last:
        log.info("nessuna nuova soglia superata.")
        return 0

    msg = (
        f"💰 <b>Guadagno {cfg.epic}: {floating:+.2f}€ ({gain_pct:+.0f}% del conto)</b>\n"
        f"Hai superato la soglia +{crossed:.0f}%.\n\n"
        f"Se vuoi <b>consolidare</b> questo guadagno puoi chiudere da /posizioni. "
        f"Ricorda pero': cosi' <b>esci dal mercato</b> e il controller potrebbe "
        f"riaprire domani. Il guadagno flottante fa gia' crescere l'equity e la "
        f"capacita' di blocchi da solo. <i>La decisione e' tua.</i>"
    )
    print("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    if dry:
        print("(dry-run: non inviato)")
        return 0
    TelegramClient(v1).send_message(msg)
    db.insert_monitoring_event({
        "event_type": "profit_alert", "reason": f"soglia +{crossed:.0f}% superata",
        "details": {"threshold": crossed, "gain_pct": round(gain_pct, 2),
                    "floating_eur": round(floating, 2)},
    })
    log.info("profit_alert inviato (soglia +%.0f%%)", crossed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
