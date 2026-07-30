"""Gate pre-registrato macro_variance_class (docs/gate-macro-variance.md).

Sola lettura: abbina le classificazioni (monitoring_events, event_type=
macro_variance) al ritorno realizzato US500 del giorno e alla baseline EWMA,
calcola M1 (ordinamento |ret| per classe) e M2 (surprise = |ret|/sigma_EWMA
del giorno prima). Stampa lo stato; a campione maturo (>=40 abbinate, >=5 high)
emette il verdetto e avvisa su Telegram. Criteri IMMUTABILI, decisi 2026-07-30.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.macro_variance_gate [--quiet]
Cron: domenica 20:10 IT (silenzioso finche' immaturo).
"""
from __future__ import annotations

import logging
import math
import sys
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from src.config import load_config

log = logging.getLogger(__name__)

MIN_TOTAL = 40
MIN_HIGH = 5
EWMA_LAMBDA = 0.94          # stessa del controller (src/volatility.py)
NOT_EVALUABLE_WEEKS = 12    # high < MIN_HIGH dopo 12 settimane -> non discrimina

ROME = ZoneInfo("Europe/Rome")


def _daily_sigma_by_date(closes_by_date: dict[str, float]) -> dict[str, float]:
    """sigma EWMA GIORNALIERA al giorno d (inclusi i ritorni fino a d compreso)."""
    dates = sorted(closes_by_date)
    sig2, out, prev = None, {}, None
    for d in dates:
        px = closes_by_date[d]
        if prev is not None and prev > 0 and px > 0:
            r = math.log(px / prev)
            sig2 = r * r if sig2 is None else EWMA_LAMBDA * sig2 + (1 - EWMA_LAMBDA) * r * r
        if sig2 is not None:
            out[d] = math.sqrt(sig2)
        prev = px
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet = "--quiet" in sys.argv

    from src.capital_client import CapitalClient
    from src.db import Database

    cfg = load_config()
    db = Database(cfg)

    events = (db._client.table("monitoring_events")
              .select("created_at,details").eq("event_type", "macro_variance")
              .order("created_at", desc=False).limit(500).execute().data or [])
    cls_by_date = {}
    for e in events:
        d = (datetime.fromisoformat(e["created_at"].replace("Z", "+00:00"))
             .astimezone(ROME).date().isoformat())
        impact = ((e.get("details") or {}).get("impact") or "").lower()
        if impact in ("low", "medium", "high"):
            cls_by_date[d] = impact   # ultima classificazione del giorno

    capital = CapitalClient(cfg)
    capital.login()
    closes_by_date = {}
    for p in capital.get_prices("US500", resolution="DAY", max_bars=300):
        cp = p.get("closePrice") or {}
        ts = (p.get("snapshotTimeUTC") or p.get("snapshotTime") or "")[:10]
        if ts and cp.get("bid") and cp.get("ask"):
            closes_by_date[ts] = (float(cp["bid"]) + float(cp["ask"])) / 2
    sigma_by_date = _daily_sigma_by_date(closes_by_date)
    dates = sorted(closes_by_date)

    # abbina: classe del giorno d -> |ret| close(d-1)->close(d), surprise vs sigma(d-1)
    paired = []
    for i, d in enumerate(dates):
        if d not in cls_by_date or i == 0:
            continue
        prev_d = dates[i - 1]
        if prev_d not in sigma_by_date:
            continue
        ret = abs(math.log(closes_by_date[d] / closes_by_date[prev_d]))
        paired.append({"date": d, "impact": cls_by_date[d], "abs_ret": ret,
                       "surprise": ret / sigma_by_date[prev_d]})

    by_cls = {c: [p for p in paired if p["impact"] == c] for c in ("low", "medium", "high")}
    n, n_high = len(paired), len(by_cls["high"])
    first = min(cls_by_date) if cls_by_date else "-"
    weeks = ((datetime.now(ROME).date() - datetime.fromisoformat(first).date()).days / 7
             if cls_by_date else 0.0)

    lines = [f"[MACRO-GATE] abbinate {n} (low {len(by_cls['low'])} / med "
             f"{len(by_cls['medium'])} / high {n_high}) | dal {first} ({weeks:.1f} settimane)"]
    for c in ("low", "medium", "high"):
        if by_cls[c]:
            lines.append(f"  {c:<6} mediana |ret| {median(p['abs_ret'] for p in by_cls[c])*100:.2f}%"
                         f"  mediana surprise {median(p['surprise'] for p in by_cls[c]):.2f}")

    verdict = None
    if n >= MIN_TOTAL and n_high >= MIN_HIGH:
        m1 = (median(p["abs_ret"] for p in by_cls["high"])
              > median(p["abs_ret"] for p in by_cls["low"])) if by_cls["low"] else False
        m2 = (median(p["surprise"] for p in by_cls["high"])
              > median(p["surprise"] for p in by_cls["low"])) if by_cls["low"] else False
        verdict = "PASS" if (m1 and m2) else "FAIL"
        lines.append(f"CAMPIONE MATURO. M1(|ret| high>low)={m1} M2(surprise high>low)={m2}"
                     f" -> {verdict}")
        lines.append("PASS: si puo' PROPORRE esperimento MACRO_SCALE (pre-registrato, "
                     "flag separato). FAIL: rimuovere cron macro_variance_class."
                     if verdict == "PASS" else
                     "FAIL: la classe non aggiunge nulla oltre l'EWMA. Rimuovere il "
                     "cron macro_variance_class; MACRO_SCALE_ENABLED mai attivo.")
    elif weeks >= NOT_EVALUABLE_WEEKS and n_high < MIN_HIGH:
        verdict = "NON_VALUTABILE"
        lines.append(f"NON VALUTABILE dopo {weeks:.0f} settimane: high {n_high} < "
                     f"{MIN_HIGH}, il classificatore non discrimina -> equivale a FAIL.")
    else:
        lines.append(f"campione immaturo (servono >= {MIN_TOTAL} abbinate e >= "
                     f"{MIN_HIGH} high, o {NOT_EVALUABLE_WEEKS} settimane): nessun verdetto.")

    msg = "\n".join(lines)
    print(msg)
    if verdict and not quiet:
        from src.telegram_client import TelegramClient
        esc = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            TelegramClient(cfg).send_message("<pre>" + esc + "</pre>")
        except Exception:
            log.exception("invio Telegram fallito")
    return 0


if __name__ == "__main__":
    sys.exit(main())
