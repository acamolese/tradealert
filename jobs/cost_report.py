"""Report costi LLM per scan, via Telegram.

Consolida il costo per scan post-deploy della pipeline two-call (Sprint 4 t1,
LIVE dal 2026-06-10) confrontandolo col baseline del vecchio modo. Sola lettura
del DB (llm_usage + scanner_runs), nessun impatto su trading/monitor.

Esecuzione:
    python -m jobs.cost_report            # invia su Telegram
    python -m jobs.cost_report --dry-run  # stampa su stdout, no Telegram

Cron suggerito: lunedi' 08:00 Europe/Rome (copre i 7 giorni precedenti).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import Database
from src.telegram_client import TelegramClient

REPORT_DAYS = 7
# Baseline vecchio modo "pulito" (1 chiamata scanner/scan, niente test di
# ricerca sopra): finestra storica fissa. Vedi conversazione 2026-06-10.
BASELINE_FROM = "2026-05-28"
BASELINE_TO = "2026-06-06"  # esclusivo (fino al 05/06)
TWO_CALL_LIVE = "2026-06-10"


def _agg(db: Database, frm: str, to: str):
    """Ritorna (per_day, totals) su [frm, to). per_day[date] = {scan, cost,
    scanner, thesis, monitor}. totals analogo aggregato."""
    usage = (db._client.table("llm_usage").select("ran_at,caller,cost_usd")
             .gte("ran_at", frm).lt("ran_at", to).execute().data)
    runs = (db._client.table("scanner_runs").select("ran_at")
            .gte("ran_at", frm).lt("ran_at", to).execute().data)
    per_day = defaultdict(lambda: {"scan": 0, "cost": 0.0, "scanner": 0.0,
                                    "thesis": 0.0, "monitor": 0.0})
    for r in runs:
        per_day[r["ran_at"][:10]]["scan"] += 1
    for u in usage:
        d = per_day[u["ran_at"][:10]]
        c = u.get("cost_usd") or 0.0
        caller = u.get("caller") or ""
        d["cost"] += c
        if caller == "scanner":
            d["scanner"] += c
        elif caller == "scanner_thesis":
            d["thesis"] += c
        elif caller == "monitor":
            d["monitor"] += c
    tot = {"scan": 0, "cost": 0.0, "scanner": 0.0, "thesis": 0.0, "monitor": 0.0}
    for d in per_day.values():
        for k in tot:
            tot[k] += d[k]
    return per_day, tot


def _cps(tot):
    return (tot["cost"] / tot["scan"]) if tot["scan"] else 0.0


def build_report(db: Database, now: datetime) -> str:
    start = (now - timedelta(days=REPORT_DAYS)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    per_day, tot = _agg(db, start, end)
    _, base = _agg(db, BASELINE_FROM, BASELINE_TO)

    cps_base = _cps(base)
    # Metrica di consolidamento: solo i giorni con two-call attiva nella finestra.
    tc = {"scan": 0, "cost": 0.0}
    for d, x in per_day.items():
        if d >= TWO_CALL_LIVE:
            tc["scan"] += x["scan"]
            tc["cost"] += x["cost"]
    cps_tc = (tc["cost"] / tc["scan"]) if tc["scan"] else 0.0
    delta = ((cps_tc - cps_base) / cps_base * 100) if (cps_base and tc["scan"]) else 0.0
    arrow = "📉" if delta < 0 else ("📈" if delta > 0 else "➡️")
    n_tc_days = sum(1 for d in per_day if d >= TWO_CALL_LIVE)

    lines = [f"📊 <b>Report costi LLM</b> (ultimi {REPORT_DAYS}gg)", ""]
    lines.append("<code>giorno  scan   tot$  $/scan</code>")
    for d in sorted(per_day):
        x = per_day[d]
        cps = (x["cost"] / x["scan"]) if x["scan"] else 0.0
        tag = " *" if d >= TWO_CALL_LIVE else ""
        lines.append(f"<code>{d[5:]}  {x['scan']:>4}  {x['cost']:>5.2f}  {cps:>5.3f}</code>{tag}")
    lines.append("")
    if tc["scan"]:
        lines.append(f"➡️ <b>Two-call ({n_tc_days}gg puliti):</b> "
                     f"<b>${cps_tc:.3f}/scan</b> ({tc['scan']} scan)")
    lines.append(f"Baseline vecchio modo (28/05-05/06): ${cps_base:.3f}/scan")
    if tc["scan"]:
        lines.append(f"Variazione vs baseline: <b>{delta:+.0f}%</b> {arrow}")
    if n_tc_days < REPORT_DAYS:
        lines.append("<i>⚠ finestra non ancora tutta two-call: cifra preliminare "
                     "finche' i 7gg non sono tutti dopo il 10/06.</i>")
    lines.append("")
    lines.append(f"Breakdown periodo: scanner ${tot['scanner']:.2f}, "
                 f"thesi ${tot['thesis']:.2f}, monitor ${tot['monitor']:.2f}")
    lines.append("<i>* = giorni con pipeline two-call (thesi solo sui setup che aprono)</i>")
    return "\n".join(lines)


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = load_config()
    db = Database(cfg)
    now = datetime.now(timezone.utc)
    text = build_report(db, now)
    if dry:
        print(text)
        return 0
    TelegramClient(cfg).send_message(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
