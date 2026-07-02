"""Agenda Sprint 6: avvisa su Telegram quando scattano le tappe pre-registrate.

Tre trigger (docs/sprint6-piano-scalata.md), ognuno notificato UNA volta sola
(stato persistito in logs/sprint6_agenda_state.json):

  A2  - 45 long chiusi dal 2026-05-20 -> rieseguire jobs/long_gate_analysis.py
  A3  - 15 trade chiusi sui 6 asset nuovi OPPURE 2026-08-15 -> gate paniere
  A4  - 50 trade chiusi dal 2026-07-02 -> gate di scaling del capitale
        (calcola gia' expectancy da trades.exit_r e profit factor dal pnl)

Silenzioso quando nessuna tappa e' matura (solo log con i contatori).

Cron (TZ Europe/Rome):
    20 8 * * *  cd $TRADEALERT_HOME && python -m jobs.sprint6_agenda >> logs/agenda.log 2>&1
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import date
from pathlib import Path

from src.config import load_config
from src.db import Database
from src.telegram_client import TelegramClient

log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / "sprint6_agenda_state.json"

A2_CUTOFF = "2026-05-20"
A2_TARGET_LONGS = 45
A3_NEW_ASSETS = {"EUR/USD", "AUD/USD", "GBP/USD", "Copper", "Hang Seng", "Nikkei"}
A3_TARGET_TRADES = 15
A3_DEADLINE = date(2026, 8, 15)
A3_MIN_TRADES_AT_DEADLINE = 8
A4_WINDOW_START = "2026-07-02"
A4_TARGET_TRADES = 50


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    db = Database(config)
    telegram = TelegramClient(config)
    state = _load_state()

    closed = (
        db._client.table("trades")
        .select("asset,direction,closed_at,pnl,exit_r")
        .eq("status", "closed")
        .not_.is_("pnl", "null")
        .execute()
        .data
    )

    # --- A2: gate long, trigger a 45 long chiusi dal fix bidirezionale ---
    n_long = sum(
        1 for t in closed
        if t["direction"] == "long" and (t["closed_at"] or "") >= A2_CUTOFF
    )
    if n_long >= A2_TARGET_LONGS and not state.get("a2_sent"):
        telegram.send_message(
            "📋 <b>Agenda Sprint 6 — tappa A2 matura</b>\n"
            f"Long chiusi dal fix bidirezionale: <b>{n_long}</b> (soglia {A2_TARGET_LONGS}).\n"
            "Rieseguire il gate direzionale (stesse soglie pre-registrate, "
            "in piu' breakdown Gold+Brent long vs resto):\n"
            "<code>PYTHONPATH=$PWD .venv/bin/python jobs/long_gate_analysis.py</code>\n"
            "Rif: docs/sprint6-long-gate.md"
        )
        state["a2_sent"] = True
        log.info("A2 alert inviato (n_long=%d)", n_long)

    # --- A3: gate paniere nuovi asset (15 trade o deadline 2026-08-15) ---
    n_new = sum(1 for t in closed if t["asset"] in A3_NEW_ASSETS)
    a3_by_count = n_new >= A3_TARGET_TRADES
    a3_by_date = date.today() >= A3_DEADLINE
    if (a3_by_count or a3_by_date) and not state.get("a3_sent"):
        if a3_by_count:
            head = f"Trade chiusi sui 6 asset nuovi: <b>{n_new}</b> (soglia {A3_TARGET_TRADES})."
            action = ("Confrontare expectancy_R nuovi vs core: se nuovi &lt; core -0.10R "
                      "-> rollback BASKET_FX_ENABLED/BASKET_TREND_ENABLED.")
        elif n_new < A3_MIN_TRADES_AT_DEADLINE:
            head = (f"Deadline {A3_DEADLINE} raggiunta con soli <b>{n_new}</b> trade "
                    f"sui nuovi asset (&lt;{A3_MIN_TRADES_AT_DEADLINE}).")
            action = ("Verdetto automatico pre-registrato: <b>ROLLBACK</b> "
                      "(BASKET_FX_ENABLED=false, BASKET_TREND_ENABLED=false nel .env VM).")
        else:
            head = f"Deadline {A3_DEADLINE} raggiunta, trade sui nuovi asset: <b>{n_new}</b>."
            action = ("Confrontare expectancy_R nuovi vs core: se nuovi &lt; core -0.10R "
                      "-> rollback flag paniere.")
        telegram.send_message(
            "📋 <b>Agenda Sprint 6 — tappa A3 matura (gate paniere)</b>\n"
            f"{head}\n{action}\nRif: docs/sprint6-piano-scalata.md sez. A3"
        )
        state["a3_sent"] = True
        log.info("A3 alert inviato (n_new=%d, by_date=%s)", n_new, a3_by_date)

    # --- A4: gate di scaling a 50 trade chiusi dal 2026-07-02 ---
    window = [t for t in closed if (t["closed_at"] or "") >= A4_WINDOW_START]
    n_win = len(window)
    if n_win >= A4_TARGET_TRADES and not state.get("a4_sent"):
        exit_rs = [float(t["exit_r"]) for t in window if t.get("exit_r") is not None]
        exp_r = statistics.mean(exit_rs) if exit_rs else float("nan")
        wins = sum(float(t["pnl"]) for t in window if float(t["pnl"]) > 0)
        losses = abs(sum(float(t["pnl"]) for t in window if float(t["pnl"]) < 0))
        pf = (wins / losses) if losses else float("inf")
        pnl_tot = sum(float(t["pnl"]) for t in window)
        exp_ok = exp_r >= 0.15
        pf_ok = pf >= 1.3
        telegram.send_message(
            "📋 <b>Agenda Sprint 6 — tappa A4 matura (gate di SCALING)</b>\n"
            f"Trade chiusi dal {A4_WINDOW_START}: <b>{n_win}</b>.\n"
            f"1) Expectancy: <b>{exp_r:+.3f}R</b> (soglia +0.15R) {'✅' if exp_ok else '❌'}\n"
            f"2) Profit factor: <b>{pf:.2f}</b> (soglia 1.3) {'✅' if pf_ok else '❌'}\n"
            f"3) P&L lordo finestra: {pnl_tot:+.2f} EUR — verificare a mano che "
            "resti positivo al netto dei costi LLM (cost_report).\n"
            + ("Se tutte e 3 ✅: step 1 (ACCOUNT_RISK_CAPITAL_EUR 100->300), "
               "PRIMA implementare il check uncle-point (prerequisito A4.3)."
               if exp_ok and pf_ok else
               "Condizioni non raggiunte: NO scaling, riaprire la diagnosi "
               "sui dati della finestra.")
            + "\nRif: docs/sprint6-piano-scalata.md sez. A4.3"
        )
        state["a4_sent"] = True
        log.info("A4 alert inviato (n=%d exp=%.3f pf=%.2f)", n_win, exp_r, pf)

    _save_state(state)
    log.info(
        "agenda: long %d/%d | nuovi asset %d/%d (deadline %s) | finestra scaling %d/%d",
        n_long, A2_TARGET_LONGS, n_new, A3_TARGET_TRADES, A3_DEADLINE,
        n_win, A4_TARGET_TRADES,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
