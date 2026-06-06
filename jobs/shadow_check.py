"""Controllo giornaliero dello shadow test scoring (Sprint 4 troncone 1).

Gira sulla VM (dove ci sono repo, .env Supabase, Telegram). Legge gli
eventi ``scoring_shadow`` da monitoring_events (reale temp 1.0 vs shadow
temp 0.2), conta i casi BORDERLINE (almeno uno score in 6.5-7.5) e:

  - se i borderline sono < SOGLIA_CAMPIONE: logga solo l'avanzamento,
    niente Telegram (no rumore quotidiano);
  - se >= SOGLIA_CAMPIONE: produce il REPORT comportamentale (quanti trade
    in meno/piu' aprirebbe temp 0.2 vs temp 1.0 sugli stessi scan), lo
    manda su Telegram con la raccomandazione e il promemoria di spegnere
    SCORING_SHADOW, e lo scrive in logs/shadow_report.txt.

Sola lettura/analisi: non tocca il sistema, non apre nulla. NO-DEPLOY
della pipeline a due chiamate finche' non c'e' il verdetto qui.

Cron suggerito (TZ Europe/Rome), dopo l'ultimo scan del giorno:
    30 22 * * *  cd $TRADEALERT_HOME && python -m jobs.shadow_check >> logs/shadow_check.log 2>&1
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from src.config import load_config
from src.db import Database
from src.telegram_client import TelegramClient

log = logging.getLogger(__name__)

SOGLIA_CAMPIONE = 15  # borderline minimi prima di produrre il report
BL_LO, BL_HI = 6.5, 7.5  # fascia borderline


def _scores(props: list[dict]) -> list[float]:
    out = []
    for p in props or []:
        s = p.get("score")
        if s is not None:
            try:
                out.append(float(s))
            except (TypeError, ValueError):
                pass
    return out


def _is_borderline(real: list[dict], shadow: list[dict]) -> bool:
    return any(BL_LO <= s <= BL_HI for s in _scores(real) + _scores(shadow))


def _build_report(events: list[dict], min_score_default: float = 7.0) -> str:
    n_real_open = n_shadow_open = 0
    agree = 0
    temp02_fewer = 0  # reale apre, shadow no
    temp02_more = 0  # reale no, shadow apre
    bl_shifts = []  # shift shadow-reale sul top, sui casi borderline
    n = 0
    for e in events:
        d = e.get("details") or {}
        real, shadow = d.get("real") or [], d.get("shadow") or []
        rs, ss = _scores(real), _scores(shadow)
        if not rs and not ss:
            continue
        n += 1
        ms = float(d.get("min_score") or min_score_default)
        r_top = max(rs) if rs else None
        s_top = max(ss) if ss else None
        r_open = r_top is not None and r_top >= ms
        s_open = s_top is not None and s_top >= ms
        n_real_open += int(r_open)
        n_shadow_open += int(s_open)
        if r_open == s_open:
            agree += 1
        elif r_open and not s_open:
            temp02_fewer += 1
        elif s_open and not r_open:
            temp02_more += 1
        if _is_borderline(real, shadow) and r_top is not None and s_top is not None:
            bl_shifts.append(s_top - r_top)

    net = n_shadow_open - n_real_open
    agree_pct = (100.0 * agree / n) if n else 0.0
    avg_shift = (sum(bl_shifts) / len(bl_shifts)) if bl_shifts else 0.0

    if net < 0:
        head = f"temp 0.2 aprirebbe <b>{abs(net)} trade in MENO</b> ({n_real_open}→{n_shadow_open} su {n} scan)"
    elif net > 0:
        head = f"temp 0.2 aprirebbe <b>{net} trade in PIU'</b> ({n_real_open}→{n_shadow_open} su {n} scan)"
    else:
        head = f"temp 0.2 aprirebbe <b>lo stesso numero</b> di trade ({n_real_open} su {n} scan)"

    # Raccomandazione: deploy se accordo alto e nessuno shift sistematico verso
    # meno open. Soglie prudenti.
    if agree_pct >= 90 and abs(net) <= 1:
        reco = "✅ <b>DEPLOY</b>: temp 0.2 apre sostanzialmente lo stesso set, scoring piu' stabile a parita' di decisioni."
    else:
        reco = "🟡 <b>NO-DEPLOY / capire prima</b>: temp 0.2 cambia il set di trade aperti in modo non trascurabile."

    return (
        f"📊 <b>Shadow scoring — report</b> ({n} scan, {len(bl_shifts)} borderline)\n"
        f"{head}\n"
        f"Accordo apri/non-apri: <b>{agree_pct:.0f}%</b>\n"
        f"Discordi: temp0.2 apre meno ×{temp02_fewer}, apre piu' ×{temp02_more}\n"
        f"Shift medio shadow-reale sui borderline: <code>{avg_shift:+.2f}</code>\n\n"
        f"{reco}\n\n"
        f"Segnale preliminare atteso: temp 0.2 leggermente piu' bassa → meno open. "
        f"Il dato {'CONFERMA' if net < 0 else 'NON conferma'} l'attesa.\n"
        f"Limiti: campione piccolo, solo finestra scan, universo 5 asset.\n\n"
        f"⚠️ <b>Spegni ora SCORING_SHADOW</b> (SCORING_SHADOW=false nel .env della VM, "
        f"niente redeploy): finche' attivo RADDOPPIA il costo scanner."
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    db = Database(config)
    events = (
        db._client.table("monitoring_events")
        .select("created_at,details")
        .eq("event_type", "scoring_shadow")
        .order("created_at")
        .execute()
        .data
    )
    borderline = [
        e
        for e in events
        if _is_borderline(
            (e.get("details") or {}).get("real") or [],
            (e.get("details") or {}).get("shadow") or [],
        )
    ]
    n_bl = len(borderline)
    log.info(
        "shadow: %d/%d borderline accumulati (%d eventi totali)",
        n_bl,
        SOGLIA_CAMPIONE,
        len(events),
    )
    if n_bl < SOGLIA_CAMPIONE:
        log.info("Campione insufficiente, niente report. Riprovo al prossimo run.")
        return 0

    report = _build_report(events)
    try:
        TelegramClient(config).send_message(report)
    except Exception:
        log.exception("Invio Telegram report shadow fallito")
    try:
        out = Path(__file__).resolve().parent.parent / "logs" / "shadow_report.txt"
        out.write_text(report, encoding="utf-8")
    except Exception:
        log.exception("Scrittura logs/shadow_report.txt fallita")
    log.info("Report shadow prodotto e inviato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
