"""Classificatore di impatto di varianza (spec §5.1). 1 chiamata LLM/giorno,
pre-apertura. NON prevede direzione (correlazione ≈ 0, spendere token li' e' nullo):
prevede VARIANZA, l'unica previsione lecita in questo impianto.

Output JSON stretto {impact: low|medium|high, reason}. Scrive in monitoring_events
(event_type=macro_variance). Il controller lo LEGGE e, con MACRO_SCALE_ENABLED off
(v1), lo LOGGA soltanto: mapping low/medium/high -> 1.0/0.7/0.5 applicato solo a
flag on, come esperimento separato (§5.1).

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.macro_variance_class [--dry-run]
"""
from __future__ import annotations

import json
import logging
import sys

from src.config import load_config

log = logging.getLogger(__name__)

PROMPT = (
    "Sei un classificatore di RISCHIO DI VARIANZA per l'indice S&P 500 (US500) nelle "
    "prossime 24 ore. NON prevedere la direzione. Stima solo QUANTA volatilita' "
    "attendere, dato il calendario macro qui sotto.\n\n"
    "Eventi nelle prossime 24h:\n{events}\n\n"
    "Rispondi SOLO con JSON valido, niente altro:\n"
    '{{"impact": "low|medium|high", "reason": "<max 20 parole>"}}\n'
    "Criterio: high = eventi ad alto impatto su volatilita' (FOMC, CPI/NFP USA, "
    "decisioni tassi maggiori); medium = eventi rilevanti ma non estremi; low = "
    "calendario tranquillo."
)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv

    from anthropic import Anthropic
    from src.db import Database
    from src.market_context import get_critical_events

    cfg = load_config()
    db = Database(cfg)

    try:
        events = get_critical_events()
    except Exception:
        log.exception("get_critical_events fallito, uso lista vuota")
        events = []
    events_txt = "\n".join(f"- {e}" for e in (events or [])) or "(nessun evento noto)"

    client = Anthropic(api_key=cfg.anthropic_api_key)
    model = getattr(cfg, "anthropic_model_fast", None) or cfg.anthropic_model
    try:
        resp = client.messages.create(
            model=model, max_tokens=120,
            messages=[{"role": "user", "content": PROMPT.format(events=events_txt)}],
        )
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        start, end = txt.find("{"), txt.rfind("}")
        data = json.loads(txt[start:end + 1]) if start >= 0 else {}
    except Exception:
        log.exception("chiamata LLM macro fallita")
        data = {"impact": "low", "reason": "classificazione fallita, default low"}

    impact = data.get("impact", "low")
    if impact not in ("low", "medium", "high"):
        impact = "low"
    reason = str(data.get("reason", ""))[:120]
    print(f"macro_variance: impact={impact} | {reason}")

    if dry:
        print("(dry-run: non scritto)")
        return 0
    db.insert_monitoring_event({
        "event_type": "macro_variance",
        "reason": reason,
        "details": {"impact": impact, "reason": reason},
    })
    log.info("macro_variance loggato: %s", impact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
