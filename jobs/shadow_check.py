"""Controllo giornaliero dello shadow test scoring (Sprint 4 troncone 1).

Gira sulla VM (dove ci sono repo, .env Supabase, Telegram). Legge gli
eventi ``scoring_shadow`` da monitoring_events e produce due viste
affiancate:
  - GREZZO (score): temp 0.2 cambia quali candidati superano la soglia?
  - NETTO GUARDRAIL (trade reali): temp 0.2 cambia quanti trade aprirei
    davvero, applicati i guardrail macro? E' la domanda comportamentale.

Il netto-guardrail usa i campi ``real_post``/``shadow_post`` loggati DAL
PIPELINE al momento dello scan (guardrail applicati con lo stato di allora:
posizioni aperte, eventi critici, calendario). Gli eventi vecchi hanno solo
``real``/``shadow`` (grezzo): contano solo nella vista grezza, il netto è
forward-only dal deploy dell'arricchimento.

Trigger: report solo con >= SOGLIA_CAMPIONE casi borderline (score grezzo
in 6.5-7.5), via Telegram, con promemoria di spegnere SCORING_SHADOW. Sola
lettura, nessuna modifica al sistema, NO-DEPLOY della pipeline a 2 chiamate.

Cron (TZ Europe/Rome), dopo l'ultimo scan del giorno:
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

SOGLIA_CAMPIONE = 15
BL_LO, BL_HI = 6.5, 7.5


def _scores(props) -> list[float]:
    out = []
    for p in props or []:
        s = p.get("score")
        if s is not None:
            try:
                out.append(float(s))
            except (TypeError, ValueError):
                pass
    return out


def _views(details: dict):
    """Estrae (real_raw, shadow_raw, real_post, shadow_post) dai details.
    real_post/shadow_post sono None per gli eventi in formato vecchio."""
    if "real_raw" in details or "shadow_raw" in details:
        return (
            _scores(details.get("real_raw")),
            _scores(details.get("shadow_raw")),
            _scores(details.get("real_post")),
            _scores(details.get("shadow_post")),
        )
    # formato vecchio: solo grezzo
    return (_scores(details.get("real")), _scores(details.get("shadow")), None, None)


def _is_borderline(details: dict) -> bool:
    rr, sr, _rp, _sp = _views(details)
    return any(BL_LO <= s <= BL_HI for s in rr + sr)


def _open(scores: list[float] | None, ms: float):
    """True/False se aprirebbe (max score >= soglia); None se dato assente."""
    if scores is None:
        return None
    return (max(scores) >= ms) if scores else False


def _tally(events, level: str, min_score_default: float = 7.0):
    """level: 'raw' o 'post'. Ritorna metriche di accordo apri/non-apri."""
    n = n_real = n_shadow = agree = fewer = more = 0
    for e in events:
        d = e.get("details") or {}
        rr, sr, rp, sp = _views(d)
        ms = float(d.get("min_score") or min_score_default)
        if level == "raw":
            ro, so = _open(rr, ms), _open(sr, ms)
        else:
            ro, so = _open(rp, ms), _open(sp, ms)
        if ro is None or so is None:
            continue  # evento senza dati per questo livello (es. vecchio formato, post)
        n += 1
        n_real += int(ro)
        n_shadow += int(so)
        if ro == so:
            agree += 1
        elif ro and not so:
            fewer += 1
        elif so and not ro:
            more += 1
    return {
        "n": n, "real_open": n_real, "shadow_open": n_shadow,
        "net": n_shadow - n_real, "agree": agree,
        "agree_pct": (100.0 * agree / n) if n else 0.0,
        "fewer": fewer, "more": more,
    }


def _fmt_level(titolo: str, m: dict) -> str:
    if m["n"] == 0:
        return f"<b>{titolo}</b>: nessun dato ancora."
    net = m["net"]
    if net < 0:
        verdict = f"{abs(net)} trade in MENO"
    elif net > 0:
        verdict = f"{net} trade in PIU'"
    else:
        verdict = "stesso numero"
    return (
        f"<b>{titolo}</b> (n={m['n']}): temp0.2 → <b>{verdict}</b> "
        f"({m['real_open']}→{m['shadow_open']})\n"
        f"  accordo {m['agree_pct']:.0f}% · discordi: meno ×{m['fewer']}, piu' ×{m['more']}"
    )


def _build_report(events) -> str:
    raw = _tally(events, "raw")
    post = _tally(events, "post")
    n_bl = sum(1 for e in events if _is_borderline(e.get("details") or {}))

    # Raccomandazione basata sul NETTO (la domanda comportamentale). Se il
    # netto non ha ancora dati, fallback prudente su no-deploy.
    if post["n"] >= SOGLIA_CAMPIONE and post["agree_pct"] >= 90 and abs(post["net"]) <= 1:
        reco = "✅ <b>DEPLOY</b>: a livello di trade reali (netto guardrail) temp0.2 apre lo stesso set, scoring piu' stabile."
    elif post["n"] < SOGLIA_CAMPIONE:
        reco = "🟡 <b>NO-DEPLOY (per ora)</b>: il netto-guardrail non ha ancora abbastanza dati (forward-only dall'arricchimento)."
    else:
        reco = "🟡 <b>NO-DEPLOY / capire prima</b>: a livello di trade reali temp0.2 cambia il set aperto."

    return (
        f"📊 <b>Shadow scoring — report</b> ({len(events)} eventi, {n_bl} borderline)\n\n"
        f"{_fmt_level('GREZZO (temp cambia lo score?)', raw)}\n\n"
        f"{_fmt_level('NETTO GUARDRAIL (temp cambia i trade reali?)', post)}\n\n"
        f"{reco}\n\n"
        f"Atteso: temp0.2 leggermente piu' bassa → potenzialmente meno open. "
        f"Netto: {'CONFERMA' if post['n'] and post['net'] < 0 else 'non conferma/insufficiente'}.\n"
        f"Limiti: campione piccolo, solo finestra scan, universo 5 asset; netto = post macro-guardrail "
        f"(non include dedup/market/affordability).\n\n"
        f"⚠️ <b>Spegni ora SCORING_SHADOW</b> (=false nel .env VM, niente redeploy): "
        f"finche' attivo RADDOPPIA il costo scanner."
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
    n_bl = sum(1 for e in events if _is_borderline(e.get("details") or {}))
    n_post = sum(
        1 for e in events
        if "real_raw" in (e.get("details") or {})
    )
    log.info(
        "shadow: %d/%d borderline (%d eventi totali, %d col netto-guardrail)",
        n_bl, SOGLIA_CAMPIONE, len(events), n_post,
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
