"""Registra nel DB la modifica F_BUDGET_POS (spec §6.2/§6.4): experiment +
adversary_review + constants_log. Idempotente: se il record constants_log
F_BUDGET_POS esiste gia', non fa nulla. Dettagli in docs/spinner-f-budget.md.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.spinner_constants_log
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone

from src.config import load_config
from src.db import Database

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = Database(cfg)
    sp = db._client.schema("spinner")

    existing = (sp.table("constants_log").select("id")
                .eq("name", "F_BUDGET_POS").execute().data or [])
    if existing:
        print(f"constants_log F_BUDGET_POS gia' presente (id {existing[0]['id']}): niente da fare.")
        return 0

    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      text=True).strip()
    except Exception:
        sha = "unknown"

    exp = db._client.table("experiment").insert({
        "question": ("F_BUDGET_POS=0.5 (budget di leva per posizione) aumenta la g "
                     "attesa di conto senza churn eccessivo, rispetto al riempimento "
                     "a F_MAX_POS che rendeva MAX_POSITIONS irraggiungibile?"),
        "metric": ("(1) somma g_exec del target vs g della migliore singola "
                   "vecchia-config, per scan; (2) mosse esecutore/settimana"),
        "gate": ("valutazione 2026-09-30: PASS se somma g >= singola nel >=70% "
                 "degli scan E mosse <= 2/settimana; FAIL su una -> rollback "
                 "F_BUDGET_POS=F_MAX_POS. Niente P&L euro (demo, pnl non riconciliato)"),
        "preregistered_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": sha,
    }).execute().data
    exp_id = exp[0]["id"]

    db._client.table("adversary_review").insert({
        "experiment_id": exp_id,
        "config_count": 1,   # una sola config candidata, non calibrata (identita' F_MAX_ACCOUNT/MAX_POSITIONS)
        "benchmark_decomp": {
            "board": "2026-07-29", "equity": 100,
            "vecchia_config_g": 0.0403, "nuova_config_g": 0.0462,
            "limite_dichiarato": "somma g non modella la covarianza tra classi",
            "obiezioni": "correlazione intra-classe (mitigata da MAX_PER_CLASS=1), "
                         "churn (gate metrica 2), taglie deboli (G_MIN alla taglia aperta)",
        },
        "noise_ceiling": 0.0,   # modifica analitica, nessun fit su dati storici
        "verdict": "approve",
    }).execute()

    sp.table("constants_log").insert({
        "name": "F_BUDGET_POS",
        "old_value": None,   # costante nuova: prima il riempimento era a F_MAX_POS
        "new_value": "0.5 (= F_MAX_ACCOUNT/MAX_POSITIONS; costruzione portafoglio "
                     "a budget per posizione, eccezione ticket lumpy, G_MIN alla "
                     "taglia aperta; misura del tabellone invariata)",
        "experiment_id": exp_id,
        "note": "docs/spinner-f-budget.md; commit " + sha,
    }).execute()

    print(f"registrato: experiment {exp_id} + adversary_review + constants_log F_BUDGET_POS (sha {sha})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
