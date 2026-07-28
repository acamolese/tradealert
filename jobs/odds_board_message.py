"""Tabellone serale [ODDS] (spec §5). Informativo: nessun denaro.

Sempre: conteggio universo/eseguibili/eligible; sezione CARRY (financing negativo,
anche se non produce eligible); contatore settimane a set vuoto (§9); mai un P&L in
euro non riconciliato.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.odds_board_message [--dry-run]
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.spinner_config import load_spinner_config

log = logging.getLogger(__name__)
CACHE = Path(__file__).resolve().parent.parent / "data" / "spinner_universe.json"


def _empty_weeks(sp) -> int:
    """Settimane consecutive (dalla piu' recente) con 0 eligible."""
    rows = (sp.table("odds_board").select("scan_date,status")
            .order("scan_date", desc=True).limit(2000).execute().data)
    by_week: dict = {}
    for r in rows:
        d = datetime.fromisoformat(r["scan_date"]).date()
        wk = d - timedelta(days=d.weekday())
        by_week.setdefault(wk, 0)
        if r["status"] == "eligible":
            by_week[wk] += 1
    n = 0
    for wk in sorted(by_week, reverse=True):
        if by_week[wk] == 0:
            n += 1
        else:
            break
    return n


def build(sp) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = sp.table("odds_board").select("*").eq("scan_date", today).execute().data
    universe = len(json.loads(CACHE.read_text())) if CACHE.exists() else 0
    executable = len({r["epic"] for r in rows})
    elig = sorted([r for r in rows if r["status"] == "eligible"],
                  key=lambda r: -(r["g_exec"] or 0))
    carry = sorted([r for r in rows if (r.get("fin_annual") or 0) < 0],
                   key=lambda r: (r.get("fin_annual") or 0))

    L = [f"[ODDS] Tabellone {today}",
         f"Universo: {universe} | eseguibili a f≤1: {executable} | eligible: {len(elig)}", ""]

    L.append("TOP (g attesa annua alla taglia eseguibile)")
    if elig:
        for i, r in enumerate(elig[:6], 1):
            L.append(f" {i}. {r['epic']:<10} {r['side']:<5} g {r['g_exec']*100:+.1f}%  "
                     f"f {r['f_exec']:.1f}  net {r['net_adj']*100:+.1f}%  "
                     f"fin {(r['fin_annual'] or 0)*100:+.1f}%  spr {r['spread_bps'] or 0:.0f}bp")
    else:
        L.append(" (nessuno sopra soglia)")
    L.append("")

    L.append("CARRY RICEVUTO (financing negativo, qualunque verso)")
    if carry:
        for r in carry[:5]:
            tag = "eligible" if r["status"] == "eligible" else f"NON eligible ({r['status']})"
            gtxt = f"g {r['g_exec']*100:+.1f}%" if r.get("g_exec") is not None else "g n/d"
            L.append(f" {r['epic']} {r['side']} riceve {abs(r['fin_annual'])*100:.1f}% "
                     f"→ net {r['net_adj']*100:+.1f}%, {gtxt}, {tag}")
    else:
        L.append(" (nessun financing negativo tra gli eseguibili)")
    L.append("")

    tgt = sp.table("target_portfolio").select("*").execute().data
    L.append("IN PORTAFOGLIO")
    if tgt:
        for t in tgt:
            L.append(f" {t['epic']} {t['side']} f {t['f_exec']:.1f}, g {t['g_exec']*100:+.1f}% ({t['reason']})")
    else:
        L.append(" FLAT (nessuna posizione target)")

    L.append(f"\nSet eligible vuoto da {_empty_weeks(sp)} settimane. EXECUTION_TARGET="
             f"{load_spinner_config().execution_target}.")
    L.append("Modello costi in verifica (financing demo §12.8): numeri provvisori.")
    return "\n".join(L)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv
    from src.db import Database
    from src.telegram_client import TelegramClient
    v1 = load_config()
    db = Database(v1)
    msg = build(db._client.schema("spinner"))
    print(msg)
    if dry:
        print("\n(dry-run: non inviato)")
        return 0
    # Fase 1: stesso bot, prefisso [ODDS] gia' nel testo (in <pre> per il monospazio).
    # Escape HTML COMPLETO: & < > vanno tutti convertiti o Telegram rifiuta il parse
    # (Bad Request: can't parse entities) e il messaggio non parte.
    esc = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    TelegramClient(v1).send_message("<pre>" + esc + "</pre>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
