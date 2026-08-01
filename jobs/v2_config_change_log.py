"""Registra nel DB l'evolutiva aggressiva v2 del 2026-08-01 (§6.4): experiment +
adversary_review + prod_config_change (4 parametri). Idempotente sul param
BLOCK_MARGIN_EUR. Dettagli e numeri in docs/v2-evolutiva-aggressiva.md.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.v2_config_change_log
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone

from src.config import load_config
from src.db import Database

log = logging.getLogger(__name__)

CHANGES = [
    ("BLOCK_MARGIN_EUR", "20", "5"),
    ("SIGMA_TARGET", "0.15", "0.20"),
    ("MAX_SCALE", "2.0", "3.0"),
    ("KILL_EQUITY_FLOOR_EUR", "40", "25"),
]


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(load_config())

    existing = (db._client.table("prod_config_change").select("id")
                .eq("param", "BLOCK_MARGIN_EUR").eq("new_value", "5")
                .execute().data or [])
    if existing:
        print(f"cambio gia' registrato (id {existing[0]['id']}): niente da fare.")
        return 0

    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      text=True).strip()
    except Exception:
        sha = "unknown"

    exp = db._client.table("experiment").insert({
        "question": ("Evolutiva aggressiva senza deposito (equity 55.63€, stallo "
                     "N_max=0): micro-blocchi 5€ + SIGMA_TARGET 0.20 + MAX_SCALE 3 "
                     "+ floor 25€ rendono v2 operativo con rischio accettato "
                     "esplicitamente dall'utente?"),
        "metric": ("backtest 14.1y US500 daily, meccanica a blocchi reale: CAGR "
                   "+4.43%, maxDD 49%, 91% investito, 128 mosse; mai HALT con "
                   "floor 25. Config blocchi >=200€: HALT a ogni floor testato"),
        "gate": ("forward: v2 deve RIENTRARE al primo target stabile e non fare "
                 "piu' di ~2 mosse/settimana; revisione se equity < 35€ (meta' "
                 "strada verso il floor). Rollback = 4 valori .env "
                 "(.env.bak-pre-aggressive)"),
        "preregistered_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": sha,
    }).execute().data
    exp_id = exp[0]["id"]

    db._client.table("adversary_review").insert({
        "experiment_id": exp_id,
        "config_count": 7,   # A..G testate nel backtest, F scelta
        "benchmark_decomp": {
            "harness": "jobs/exposure_aggressive_test.py, start 55.63, 14.1y",
            "scelta_F": {"cagr": 0.0443, "maxdd": 0.49, "pct_inv": 0.91},
            "alternative": {"E_prudente": 0.0410, "G_max": 0.0480,
                            "blocchi_200": "HALT", "config_attuale": "stallo 0 mosse"},
            "bh_400": {"cagr": 0.1191, "maxdd": 1.287, "nota": "non sopravvive"},
            "rischio_accettato": "utente 2026-08-01: aggressivo nel rischio, "
                                 "non nel capitale; floor 40->25 = il conto puo' "
                                 "dimezzarsi prima dell'HALT",
            "non_toccato": "GAP_TOLERANCE 0.10 (sopravvivenza al gap): G la "
                           "indeboliva per +0.37%/anno, rifiutato",
        },
        "noise_ceiling": 0.0037,  # delta CAGR F-G: sotto, le differenze sono rumore
        "verdict": "approve",
    }).execute()

    for param, old, new in CHANGES:
        db._client.table("prod_config_change").insert({
            "experiment_id": exp_id, "param": param,
            "old_value": old, "new_value": new,
        }).execute()

    print(f"registrato: experiment {exp_id} + adversary_review + "
          f"{len(CHANGES)} prod_config_change (sha {sha})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
