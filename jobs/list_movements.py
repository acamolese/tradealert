"""Elenco rapido dei movimenti (trade) per analisi degli ultimi setup.

Uso:
    .venv/bin/python -m jobs.list_movements                # ultimi 7 giorni
    .venv/bin/python -m jobs.list_movements --since 2026-05-16
    .venv/bin/python -m jobs.list_movements --days 14
    .venv/bin/python -m jobs.list_movements --since 2026-05-16 --signals

Mostra i trade aperti e/o chiusi nella finestra, con P&L e exit_reason.
Con --signals aggiunge anche i signal generati nello stesso periodo
(utile per analizzare i setup proposti dall'LLM, anche quelli skippati).

Va eseguito lato server (VM) dove e' presente la service_role key:
con RLS abilitato la sola anon key restituirebbe liste vuote.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from supabase import create_client

from src.config import load_config


def _fmt_num(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _parse_since(args: argparse.Namespace) -> str:
    if args.since:
        return args.since
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    return cutoff.date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Elenco movimenti TradeAlert")
    parser.add_argument("--since", help="Data inizio inclusa, formato YYYY-MM-DD")
    parser.add_argument(
        "--days", type=int, default=7, help="Finestra in giorni se --since assente (default 7)"
    )
    parser.add_argument(
        "--signals", action="store_true", help="Includi anche i signal del periodo"
    )
    args = parser.parse_args()

    since = _parse_since(args)
    config = load_config()
    key = config.supabase_service_role_key or config.supabase_anon_key
    if not config.supabase_service_role_key:
        print(
            "ATTENZIONE: service_role key assente, uso anon key. "
            "Con RLS abilitato i risultati saranno vuoti.",
            file=sys.stderr,
        )
    client = create_client(config.supabase_url, key)

    opened = (
        client.table("trades")
        .select("*")
        .gte("opened_at", since)
        .order("opened_at")
        .execute()
        .data
    )
    closed = (
        client.table("trades")
        .select("*")
        .gte("closed_at", since)
        .order("closed_at")
        .execute()
        .data
    )

    print(f"=== Movimenti dal {since} ===\n")

    print(f"Trade APERTI nel periodo: {len(opened)}")
    for t in opened:
        print(
            f"  #{t['id']} {t['opened_at']}  {t['asset']:<12} {t['direction']:<5} "
            f"size={_fmt_num(t['size'])} entry={_fmt_num(t['entry_price'])} "
            f"sl={_fmt_num(t.get('current_sl'))} tp={_fmt_num(t.get('current_tp'))} "
            f"[{t['status']}]"
        )

    print(f"\nTrade CHIUSI nel periodo: {len(closed)}")
    total_pnl = 0.0
    wins = 0
    for t in closed:
        pnl = t.get("pnl")
        if pnl is not None:
            total_pnl += float(pnl)
            if float(pnl) > 0:
                wins += 1
        print(
            f"  #{t['id']} {t['closed_at']}  {t['asset']:<12} {t['direction']:<5} "
            f"entry={_fmt_num(t['entry_price'])} close={_fmt_num(t.get('close_price'))} "
            f"pnl={_fmt_num(pnl)} ({_fmt_num(t.get('pnl_pct'))}%) "
            f"reason={t.get('exit_reason') or '-'}"
        )
    if closed:
        print(
            f"\n  P&L totale chiusi: {total_pnl:.2f}  |  "
            f"win {wins}/{len(closed)}"
        )

    if args.signals:
        signals = (
            client.table("signals")
            .select("*")
            .gte("created_at", since)
            .order("created_at")
            .execute()
            .data
        )
        print(f"\nSIGNAL generati nel periodo: {len(signals)}")
        for s in signals:
            print(
                f"  #{s['id']} {s['created_at']}  {s['asset']:<12} {s['direction']:<5} "
                f"score={_fmt_num(s.get('score'))} "
                f"entry={_fmt_num(s.get('entry_price'))} "
                f"sl={_fmt_num(s.get('stop_loss'))} tp={_fmt_num(s.get('take_profit'))} "
                f"[{s['status']}]"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
