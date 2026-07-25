"""Agenda Sprint 6: avvisa su Telegram quando scattano le tappe pre-registrate.

Tre trigger (docs/sprint6-piano-scalata.md), ognuno notificato UNA volta sola
(stato persistito in logs/sprint6_agenda_state.json):

  A2  - CHIUSA 2026-07-16 (3 round NON CONCLUSIVO, bleed normalizzato) -> trigger disattivato
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

# Fase B (Sprint 7): entrata deterministica LIVE dal 2026-07-14 (flag
# SCORING_LLM_OFF). Gate forward pre-registrato (docs/sprint7-fase-b.md):
# a 20 trade chiusi nati da signal "[v1-momentum]", confronto expectancy_R
# con i 20 trade LLM precedenti lo switch. PROSEGUI se det >= llm - 0.05R;
# ROLLBACK flag se det <= llm - 0.15R; in mezzo: proseguire fino a 40.
B_START = "2026-07-14"
B_TARGET_TRADES = 20
B_THESIS_MARKER = "[v1-momentum]"

A2_CUTOFF = "2026-05-20"
A2_TARGET_LONGS = 60  # tappa CHIUSA il 2026-07-16 (vedi blocco A2), tenuto solo per il log
A3_NEW_ASSETS = {"EUR/USD", "AUD/USD", "GBP/USD", "Copper", "Hang Seng", "Nikkei"}
A3_TARGET_TRADES = 15
A3_DEADLINE = date(2026, 8, 15)
A3_MIN_TRADES_AT_DEADLINE = 8
A4_WINDOW_START = "2026-07-02"
A4_TARGET_TRADES = 50
# Sprint 8: gate forward del paniere INDICI (docs/sprint8-indici-gate.md).
INDICI_START = "2026-07-25"
INDICI_TARGET = 25


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

    # --- A2: CHIUSA il 2026-07-16 (docs/sprint6-long-gate.md, round 3 n=61) ---
    # 3 round consecutivi NON CONCLUSIVO, bleed long normalizzato in modo monotono
    # (-0.134R -> -0.065R -> -0.040R). Tappa chiusa su decisione utente: long
    # tenuto a rischio pieno, nessun riarmo. Trigger disattivato (niente send);
    # n_long resta solo per il contatore di log finale.
    n_long = sum(
        1 for t in closed
        if t["direction"] == "long" and (t["closed_at"] or "") >= A2_CUTOFF
    )

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

    # --- Fase B: gate forward selettore deterministico (20 trade chiusi) ---
    n_det = 0
    if not state.get("fase_b_gate_sent"):
        det_sigs = (
            db._client.table("signals")
            .select("id")
            .gte("created_at", B_START)
            .like("thesis", B_THESIS_MARKER + "%")
            .execute()
            .data
        )
        det_ids = {s["id"] for s in det_sigs}
        det_closed = (
            db._client.table("trades")
            .select("id,signal_id,exit_r,pnl,closed_at")
            .eq("status", "closed")
            .not_.is_("pnl", "null")
            .gte("closed_at", B_START)
            .execute()
            .data
        )
        det_trades = [t for t in det_closed if t.get("signal_id") in det_ids]
        n_det = len(det_trades)
        if n_det >= B_TARGET_TRADES:
            llm_base = (
                db._client.table("trades")
                .select("exit_r,pnl,closed_at,signal_id")
                .eq("status", "closed")
                .not_.is_("pnl", "null")
                .not_.is_("signal_id", "null")
                .lt("closed_at", B_START)
                .order("closed_at", desc=True)
                .limit(B_TARGET_TRADES)
                .execute()
                .data
            )

            def _exp(rows):
                vals = [float(t["exit_r"]) for t in rows if t.get("exit_r") is not None]
                return statistics.mean(vals) if vals else float("nan")

            exp_det, exp_llm = _exp(det_trades), _exp(llm_base)
            delta = exp_det - exp_llm
            if delta >= -0.05:
                guide = "✅ PROSEGUI: il deterministico regge il confronto (gate pre-registrato)."
            elif delta <= -0.15:
                guide = ("❌ ROLLBACK pre-registrato: SCORING_LLM_OFF=false nel .env VM "
                         "(il deterministico apre peggio).")
            else:
                guide = "⚠️ Zona grigia: proseguire fino a 40 trade, nessuna azione."
            telegram.send_message(
                "📋 <b>Agenda — GATE FASE B (selettore deterministico)</b>\n"
                f"Trade chiusi dal selettore v1-momentum: <b>{n_det}</b>.\n"
                f"Expectancy: det <b>{exp_det:+.3f}R</b> vs LLM (ultimi {B_TARGET_TRADES} "
                f"pre-switch) <b>{exp_llm:+.3f}R</b> | delta {delta:+.3f}R\n"
                f"{guide}\nRif: docs/sprint7-fase-b.md"
            )
            state["fase_b_gate_sent"] = True
            log.info("Fase B gate alert inviato (n=%d, delta=%.3f)", n_det, delta)

    # --- Gate paniere INDICI: 25 trade chiusi dal ridisegno 2026-07-24 ---
    n_idx = sum(1 for t in closed if (t["closed_at"] or "") >= INDICI_START)
    if n_idx >= INDICI_TARGET and not state.get("indici_gate_sent"):
        telegram.send_message(
            "📋 <b>Agenda Sprint 8 — gate paniere INDICI maturo</b>\n"
            f"Trade chiusi dal ridisegno: <b>{n_idx}</b> (soglia {INDICI_TARGET}).\n"
            "Valutare il gate forward pre-registrato (expectancy_R vs +0.05 conferma / "
            "-0.05 rollback):\n"
            "<code>PYTHONPATH=$PWD .venv/bin/python jobs/indici_gate.py</code>\n"
            "Rif: docs/sprint8-indici-gate.md"
        )
        state["indici_gate_sent"] = True
        log.info("Gate INDICI alert inviato (n=%d)", n_idx)

    _save_state(state)
    log.info(
        "agenda: long %d/%d | nuovi asset %d/%d (deadline %s) | finestra scaling %d/%d | "
        "faseA %d/%d signal confrontati | faseB %d/%d trade det chiusi",
        n_long, A2_TARGET_LONGS, n_new, A3_TARGET_TRADES, A3_DEADLINE,
        n_win, A4_TARGET_TRADES, n_matched, A5_TARGET_SIGNALS, n_det, B_TARGET_TRADES,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
