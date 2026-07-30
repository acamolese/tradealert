"""Diagnostica TradeSpinner: perche' il target_portfolio non cresce (sola lettura).

Replica la selezione di jobs/odds_scan.py::_build_target sugli ultimi giorni di
odds_board e stampa, per ogni candidato eligible scartato, QUALE vincolo lo ha
escluso (MAX_POSITIONS, MAX_PER_CLASS, F_MAX_ACCOUNT, isteresi). Serve a separare
"nessun eligible" da "eligible ma tetto di leva aggregata saturo".

NON scrive nulla, NON contatta il broker, NON apre ordini: solo SELECT sul DB.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.spinner_diag [giorni]
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from src.config import load_config
from src.db import Database
from src.spinner_config import (
    F_MAX_ACCOUNT, MAX_POSITIONS, MAX_PER_CLASS, ENTRY_CONFIRM_SCANS, G_MIN,
)


def main() -> int:
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 4

    cfg = load_config()
    sp = Database(cfg)._client.schema("spinner")

    print(f"vincoli attivi: G_MIN={G_MIN} MAX_POSITIONS={MAX_POSITIONS} "
          f"MAX_PER_CLASS={MAX_PER_CLASS} F_MAX_ACCOUNT={F_MAX_ACCOUNT} "
          f"ENTRY_CONFIRM_SCANS={ENTRY_CONFIRM_SCANS}")

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

        # isteresi come la calcola _build_target (conta occorrenze, non consecutivita')
        recent = (sp.table("odds_board").select("scan_date,epic,side,status")
                  .neq("scan_date", d).eq("status", "eligible")
                  .order("scan_date", desc=True).limit(400).execute().data or [])
        consec: dict[tuple[str, str], int] = defaultdict(int)
        for r in recent:
            consec[(r["epic"], r["side"])] += 1
        print(f"    (storico eligible letto: {len(recent)} righe, date "
              f"{sorted({r['scan_date'] for r in recent}, reverse=True)})")

        chosen: list[dict] = []
        per_class: dict[str, int] = {}
        f_sum = 0.0
        rejects: list[tuple[str, str, str]] = []
        for e in elig:
            if len(chosen) >= MAX_POSITIONS:
                rejects.append((e["epic"], e["side"], "MAX_POSITIONS"))
                continue
            cls = e["asset_class"]
            if per_class.get(cls, 0) >= MAX_PER_CLASS:
                rejects.append((e["epic"], e["side"], f"MAX_PER_CLASS[{cls}]"))
                continue
            f = float(e["f_exec"] or 0)
            if f_sum + f > F_MAX_ACCOUNT:
                rejects.append((e["epic"], e["side"],
                                f"F_MAX_ACCOUNT[{f_sum:.2f}+{f:.2f}>{F_MAX_ACCOUNT}]"))
                continue
            confirms = consec.get((e["epic"], e["side"]), 0) + 1
            if confirms < ENTRY_CONFIRM_SCANS:
                rejects.append((e["epic"], e["side"], f"ISTERESI[{confirms}]"))
                continue
            chosen.append(e)
            per_class[cls] = per_class.get(cls, 0) + 1
            f_sum += f
        print(f"    -> SCELTI {[(c['epic'], c['side'], c['f_exec']) for c in chosen]} "
              f"f_sum={f_sum:.2f}")
        print(f"    -> scartati {len(rejects)}: "
              f"{dict(Counter(r[2].split('[')[0] for r in rejects))}")
        for r in rejects[:12]:
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
