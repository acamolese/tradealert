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

# Fase A (Sprint 7): shadow del selettore deterministico d'entrata.
# Fine fase: 20 signal LLM con evento entry_shadow nello stesso scan,
# oppure 2026-07-31. Guida pre-registrata alla lettura (concordanza
# asset+direzione sui signal confrontati): >=60% switch diretto Fase B;
# 30-60% switch con gate forward stretto (open-rate + expectancy 20 trade);
# <30% analizzare le divergenze prima dello switch.
A5_SHADOW_START = "2026-07-02T22:00:00+00:00"
A5_TARGET_SIGNALS = 20
A5_DEADLINE = date(2026, 7, 31)
A5_MATCH_WINDOW_MIN = 20

A2_CUTOFF = "2026-05-20"
A2_TARGET_LONGS = 60  # round 3 (round 2 a 45: NON CONCLUSIVO, docs/sprint6-long-gate.md)
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

    # --- Fase A: shadow selettore deterministico (20 signal confrontati o deadline) ---
    n_matched, n_conc_dir, n_conc_asset = 0, 0, 0
    if not state.get("fase_a_sent"):
        from datetime import datetime, timedelta

        sigs = (
            db._client.table("signals")
            .select("asset,direction,created_at")
            .gte("created_at", A5_SHADOW_START)
            .execute()
            .data
        )
        shadows = (
            db._client.table("monitoring_events")
            .select("created_at,details")
            .eq("event_type", "entry_shadow")
            .gte("created_at", A5_SHADOW_START)
            .execute()
            .data
        )
        win = timedelta(minutes=A5_MATCH_WINDOW_MIN)
        sh_parsed = [
            (datetime.fromisoformat(s["created_at"]), (s.get("details") or {}).get("pick"))
            for s in shadows
        ]
        for sig in sigs:
            st = datetime.fromisoformat(sig["created_at"])
            near = [p for t, p in sh_parsed if abs(t - st) <= win]
            if not near:
                continue
            n_matched += 1
            pick = near[0]
            if pick and pick.get("asset") == sig["asset"]:
                n_conc_asset += 1
                if pick.get("direction") == sig["direction"]:
                    n_conc_dir += 1
        if n_matched >= A5_TARGET_SIGNALS or date.today() >= A5_DEADLINE:
            pct_dir = (n_conc_dir / n_matched * 100) if n_matched else 0.0
            pct_asset = (n_conc_asset / n_matched * 100) if n_matched else 0.0
            if pct_dir >= 60:
                guide = ("Concordanza alta: da guida pre-registrata si puo' passare "
                         "alla Fase B (switch selettore deterministico, LLM solo monitor).")
            elif pct_dir >= 30:
                guide = ("Concordanza media: Fase B possibile ma con gate forward "
                         "stretto (open-rate + expectancy sui primi 20 trade).")
            else:
                guide = ("Concordanza bassa: analizzare le divergenze prima dello "
                         "switch (i due selettori aprono stream diversi).")
            telegram.send_message(
                "📋 <b>Agenda — FINE FASE A (shadow selettore d'entrata)</b>\n"
                f"Signal LLM confrontati: <b>{n_matched}</b> (target {A5_TARGET_SIGNALS}).\n"
                f"Concordanza asset: <b>{pct_asset:.0f}%</b> | "
                f"asset+direzione: <b>{pct_dir:.0f}%</b>\n"
                f"{guide}\nRif: docs/sprint7-fase-a-entry-shadow.md"
            )
            state["fase_a_sent"] = True
            log.info("Fase A alert inviato (n=%d, dir=%.0f%%)", n_matched, pct_dir)

    _save_state(state)
    log.info(
        "agenda: long %d/%d | nuovi asset %d/%d (deadline %s) | finestra scaling %d/%d | "
        "faseA %d/%d signal confrontati",
        n_long, A2_TARGET_LONGS, n_new, A3_TARGET_TRADES, A3_DEADLINE,
        n_win, A4_TARGET_TRADES, n_matched, A5_TARGET_SIGNALS,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
