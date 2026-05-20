"""Esporta la tabella ``trades`` completa in CSV canonico.

La sorgente di verita' resta Supabase. Questo script produce uno
snapshot versionabile (docs/trades.csv) con TUTTE le colonne del DB,
tutti i trade, ordinati per id. Va ri-eseguito per aggiornarlo.

Uso (lato VM, serve service_role key):
    .venv/bin/python -m jobs.export_trades
    .venv/bin/python -m jobs.export_trades --out /percorso/trades.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from supabase import create_client

from src.config import load_config

# Ordine canonico delle colonne, allineato allo schema di create table trades.
COLUMNS = [
    "id",
    "signal_id",
    "capital_deal_id",
    "opened_at",
    "closed_at",
    "asset",
    "direction",
    "size",
    "entry_price",
    "current_sl",
    "current_tp",
    "close_price",
    "pnl",
    "pnl_pct",
    "exit_reason",
    "status",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Export canonico trades -> CSV")
    parser.add_argument(
        "--out",
        default="docs/trades.csv",
        help="Percorso file CSV di output (default docs/trades.csv)",
    )
    args = parser.parse_args()

    config = load_config()
    if not config.supabase_service_role_key:
        print(
            "ATTENZIONE: service_role key assente, uso anon key. "
            "Con RLS abilitato l'export risultera' vuoto.",
            file=sys.stderr,
        )
    client = create_client(
        config.supabase_url,
        config.supabase_service_role_key or config.supabase_anon_key,
    )

    rows = client.table("trades").select("*").order("id").execute().data

    # Verifica che non esistano colonne nuove non previste: l'export deve
    # restare un mirror fedele dello schema, non perdere campi in silenzio.
    seen = {k for r in rows for k in r.keys()}
    extra = seen - set(COLUMNS)
    if extra:
        print(
            f"ATTENZIONE: colonne nel DB non incluse nell'export: {sorted(extra)}",
            file=sys.stderr,
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c) for c in COLUMNS})

    closed = sum(1 for r in rows if r.get("status") == "closed")
    tracked = [r for r in rows if r.get("pnl") is not None]
    net = sum(float(r["pnl"]) for r in tracked)
    print(f"Export completato: {out_path}")
    print(f"  trade totali: {len(rows)}  (chiusi {closed}, aperti {len(rows) - closed})")
    print(f"  con P&L tracciato: {len(tracked)}  |  P&L netto: {net:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
