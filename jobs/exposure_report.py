"""Report v2 (spec §10). Riscritto per il controller di esposizione: la metrica
primaria e' l'information ratio del controller CONTRO always_1_block. Vietate (non
definite su posizione persistente o gia' fuorvianti): expectancy R, win rate,
profit factor, R multipli.

Il report NON puo' mostrare il controller senza always_1_block (§6.3): se manca la
serie del benchmark, fallisce.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.exposure_report [--dry-run]
"""
from __future__ import annotations

import logging
import math
import statistics
import sys

from src.config import load_config

log = logging.getLogger(__name__)
ANN = math.sqrt(252)


def _series(ledger, strat):
    rows = sorted([r for r in ledger if r["strategy"] == strat], key=lambda r: r["as_of_date"])
    return rows


def _ir(ctrl_daily, bench_daily):
    diff = [c - b for c, b in zip(ctrl_daily, bench_daily)]
    if len(diff) < 2:
        return None
    sd = statistics.pstdev(diff)
    return (statistics.mean(diff) / sd * ANN) if sd > 0 else None


def _sharpe(daily):
    if len(daily) < 2:
        return None
    sd = statistics.pstdev(daily)
    return (statistics.mean(daily) / sd * ANN) if sd > 0 else None


def _sortino(daily):
    if len(daily) < 2:
        return None
    downside = [min(x, 0.0) for x in daily]
    dd = math.sqrt(statistics.mean([d * d for d in downside]))
    return (statistics.mean(daily) / dd * ANN) if dd > 0 else None


def _max_dd(cum):
    peak = -1e9
    mdd = 0.0
    for c in cum:
        eq = 1 + c
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return mdd


def build_report(db) -> str:
    ledger = db._client.table("shadow_ledger").select("*").order("as_of_date").execute().data
    if not ledger:
        return "📊 <b>Report v2</b>\nNessun dato nel benchmark ombra (shadow_ledger vuoto)."

    ctrl = _series(ledger, "controller")
    bench = _series(ledger, "always_1_block")
    if not bench:
        raise RuntimeError("shadow_ledger senza always_1_block: report non ammesso (§6.3)")

    # allinea per data
    bench_by_date = {r["as_of_date"]: r for r in bench}
    paired = [(c, bench_by_date[c["as_of_date"]]) for c in ctrl if c["as_of_date"] in bench_by_date]
    ctrl_daily = [float(c["daily_return"]) for c, _ in paired]
    bench_daily = [float(b["daily_return"]) for _, b in paired]
    ctrl_cum = [float(c["cum_return"]) for c in ctrl]
    bench_cum = [float(b["cum_return"]) for b in bench]

    ir = _ir(ctrl_daily, bench_daily)
    exposures = [float(c["exposure_eur"]) for c in ctrl]
    changes = sum(1 for i in range(1, len(exposures)) if exposures[i] != exposures[i - 1])
    cost_total = sum(float(c["cost_modeled"]) for c in ctrl)
    n = len(ctrl)

    def fmt(x, pct=False):
        if x is None:
            return "n/d"
        return f"{x*100:+.1f}%" if pct else f"{x:+.2f}"

    lines = [
        "📊 <b>Report v2 — controller di esposizione</b>",
        f"Giorni nel ledger: <b>{n}</b>",
        "",
        "<b>Contro il beta passivo (always_1_block):</b>",
        f"  Information ratio: <b>{fmt(ir)}</b>  ← metrica primaria",
        f"  Rendimento cum controller: {fmt(ctrl_cum[-1] if ctrl_cum else None, True)}",
        f"  Rendimento cum always_1_block: {fmt(bench_cum[-1] if bench_cum else None, True)}",
        "",
        "<b>Controller:</b>",
        f"  Sharpe: {fmt(_sharpe(ctrl_daily))} | Sortino: {fmt(_sortino(ctrl_daily))}",
        f"  Max drawdown: {fmt(_max_dd(ctrl_cum), True)}",
        f"  Esposizione media: {statistics.mean(exposures):.0f}€ | cambi di blocco: {changes}",
        f"  Costo totale modellato: {cost_total:.2f}€",
        "",
        "<i>Metriche in euro non riconciliate col conto (bug pnl DB): curve da "
        "rendimenti sul nozionale. Expectancy R/win rate/PF non riportate: non "
        "definite su posizione persistente.</i>",
    ]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv
    from src.db import Database
    from src.telegram_client import TelegramClient
    v1 = load_config()
    db = Database(v1)
    report = build_report(db)
    if dry:
        print(report)
        return 0
    TelegramClient(v1).send_message(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
