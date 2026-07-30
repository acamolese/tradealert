"""Diagnostica TradeSpinner: come viene costruito il target (sola lettura).

Usa la STESSA logica di selezione dello scan (src.spinner_odds.select_target +
jobs.odds_scan.consecutive_eligible) e stampa, per ogni eligible scartato, QUALE
vincolo lo ha escluso (MAX_POSITIONS, MAX_PER_CLASS, BUDGET_F, G_MIN_TAGLIA,
ISTERESI, EPIC_DOPPIO). Serve a separare "nessun eligible" da "eligible ma
vincoli saturi".

NON scrive nulla, NON contatta il broker, NON apre ordini: solo SELECT sul DB.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.spinner_diag [giorni]
"""
from __future__ import annotations

import sys
from collections import Counter

from src.config import load_config
from src.db import Database
from src.spinner_config import (
    F_MAX_ACCOUNT, F_MAX_POS, F_BUDGET_POS, MAX_POSITIONS, MAX_PER_CLASS,
    ENTRY_CONFIRM_SCANS, G_MIN,
)
from src.spinner_odds import select_target
from jobs.odds_scan import consecutive_eligible


def main() -> int:
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 4
    equity_diag = 100.0   # equity di lavoro (cap demo); solo per la simulazione

    cfg = load_config()
    sp = Database(cfg)._client.schema("spinner")

    print(f"vincoli: G_MIN={G_MIN} MAX_POSITIONS={MAX_POSITIONS} "
          f"MAX_PER_CLASS={MAX_PER_CLASS} F_MAX_ACCOUNT={F_MAX_ACCOUNT} "
          f"F_BUDGET_POS={F_BUDGET_POS} ENTRY_CONFIRM_SCANS={ENTRY_CONFIRM_SCANS}")

    days = sorted({r["scan_date"] for r in
                   (sp.table("odds_board").select("scan_date")
                    .order("scan_date", desc=True).limit(6000).execute().data or [])},
                  reverse=True)
    print("scan_date presenti:", days[:12])

    for d in days[:days_back]:
        rows = (sp.table("odds_board").select("*").eq("scan_date", d)
                .limit(4000).execute().data or [])
        print(f"\n=== {d} | righe {len(rows)} | status {dict(Counter(r['status'] for r in rows))}")
        elig = sorted([r for r in rows if r["status"] == "eligible"],
                      key=lambda r: -(r["g_exec"] or 0))
        print(f"    eligible {len(elig)} per classe: "
              f"{dict(Counter(r['asset_class'] for r in elig))}")
        for r in elig[:15]:
            print(f"    {r['epic']:<12} {r['side']:<5} {r['asset_class']:<13} "
                  f"g={r['g_exec']} f_exec={r['f_exec']} net_adj={r['net_adj']} "
                  f"sigma={r['sigma_ann']} minNot={r['min_notional_eur']}")

        consec = consecutive_eligible(sp, d)
        prev = (sp.table("target_portfolio").select("as_of_date,epic,side")
                .lt("as_of_date", d).order("as_of_date", desc=True)
                .limit(50).execute().data or [])
        held = ({(r["epic"], r["side"]) for r in prev
                 if r["as_of_date"] == prev[0]["as_of_date"]} if prev else set())

        chosen, rejects = select_target(
            elig, held, consec, equity_diag,
            max_positions=MAX_POSITIONS, max_per_class=MAX_PER_CLASS,
            f_max_account=F_MAX_ACCOUNT, f_max_pos=F_MAX_POS,
            f_budget=F_BUDGET_POS, g_min=G_MIN,
            entry_confirm_scans=ENTRY_CONFIRM_SCANS)
        print(f"    -> SCELTI {[(c['epic'], c['side'], c['units'], c['f_chosen'], c['g_chosen'], c['reason']) for c in chosen]} "
              f"f_sum={sum(c['f_chosen'] for c in chosen):.2f} "
              f"g_somma={sum(c['g_chosen'] for c in chosen):.4f}")
        print(f"    -> scartati {len(rejects)}: "
              f"{dict(Counter(r[2].split('[')[0] for r in rejects))}")
        for r in rejects[:14]:
            print(f"       x {r[0]:<12} {r[1]:<5} {r[2]}")

    print("\n=== target_portfolio (ultime 40 righe) ===")
    for t in (sp.table("target_portfolio").select("*")
              .order("as_of_date", desc=True).limit(40).execute().data or []):
        print(f"  {t['as_of_date']} {t['epic']:<12} {t['side']:<5} units={t['units']} "
              f"f={t['f_exec']} g={t['g_exec']} reason={t['reason']}")

    print("\n=== executor_position (ultime 20) ===")
    for r in (sp.table("executor_position").select("*")
              .order("opened_at", desc=True).limit(20).execute().data or []):
        print(f"  {r.get('opened_at')} {r['epic']:<12} {r['side']:<5} size={r['size']} "
              f"open={r.get('open_price')} closed={r.get('closed_at')} "
              f"reason={r.get('close_reason')} deal={r.get('deal_id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
