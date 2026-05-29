"""Esporta i dettagli approfonditi dei setup (signal -> trade) in EURO.

Un "setup" qui e' un signal che si e' concretizzato in un trade reale su
Capital. Lo stato del setup coincide con lo stato del trade:
  - in corso  -> trade.status = 'open'   (P&L flottante)
  - chiuso    -> trade.status = 'closed' (P&L realizzato)

Per ogni setup lo script raccoglie:
  - i dati del trade (prezzi, size, SL/TP, P&L, exit_reason)
  - il signal sorgente (score, tesi, costo atteso, feature alla decisione)
  - la cronologia dei monitoring_events (apertura, trailing, chiusura, ...)

Tutti gli importi monetari sono in EURO: il conto Capital.com e' in EUR,
quindi pnl, entry_price, close_price, expected_cost sono gia' nella valuta
del conto e non serve conversione.

Di default esporta TUTTI i setup dall'inizio in ordine cronologico.
Usa --limit N per limitarti agli ultimi N setup aperti.

Uso (lato VM, serve service_role key per via di RLS):
    .venv/bin/python -m jobs.export_setups
    .venv/bin/python -m jobs.export_setups --limit 10
    .venv/bin/python -m jobs.export_setups --status closed
    .venv/bin/python -m jobs.export_setups --json --out docs/setups.json
    .venv/bin/python -m jobs.export_setups --out docs/setups.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.config import load_config
from src.db import Database


def _eur(value: Any) -> str:
    """Formatta un importo in euro, gestendo None e segno esplicito assente."""
    if value is None:
        return "n/d"
    try:
        return f"{float(value):,.2f} EUR".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _eur_signed(value: Any) -> str:
    """Come _eur ma con segno esplicito (per P&L)."""
    if value is None:
        return "n/d"
    try:
        return f"{float(value):+,.2f} EUR".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _num(value: Any) -> str:
    if value is None:
        return "n/d"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: Any) -> str:
    if value is None:
        return "n/d"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)


def fetch_setups(db: Database, status: str, limit: int | None) -> list[dict[str, Any]]:
    """Recupera i trade (setup) filtrati per status, arricchiti con il
    signal sorgente e i monitoring_events.

    status: 'all' | 'open' | 'closed'
    limit:  se valorizzato, ultimi N per opened_at; altrimenti tutti.
    """
    query = db._client.table("trades").select("*")
    if status in ("open", "closed"):
        query = query.eq("status", status)

    # order desc + limit per prendere gli ultimi N in modo efficiente.
    query = query.order("opened_at", desc=True)
    if limit is not None:
        query = query.limit(limit)
    trades = query.execute().data or []

    # Stampa cronologica (dal piu' vecchio al piu' recente).
    trades = list(reversed(trades))

    setups: list[dict[str, Any]] = []
    for tr in trades:
        signal = None
        sig_id = tr.get("signal_id")
        if sig_id is not None:
            signal = db.get_signal(sig_id)

        events = (
            db._client.table("monitoring_events")
            .select("*")
            .eq("trade_id", tr["id"])
            .order("created_at")
            .execute()
            .data
            or []
        )
        setups.append({"trade": tr, "signal": signal, "events": events})
    return setups


def render_text(setups: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    out = lines.append

    out("=" * 72)
    out(f"EXPORT SETUP TRADEALERT  ({len(setups)} setup, importi in EURO)")
    out("=" * 72)

    realized = 0.0
    floating = 0.0
    n_closed = 0
    n_open = 0

    for i, s in enumerate(setups, 1):
        tr = s["trade"]
        sig = s["signal"]
        events = s["events"]

        status = tr.get("status")
        if status == "closed":
            n_closed += 1
            if tr.get("pnl") is not None:
                realized += float(tr["pnl"])
        else:
            n_open += 1
            if tr.get("pnl") is not None:
                floating += float(tr["pnl"])

        out("")
        out("-" * 72)
        stato_label = "CHIUSO" if status == "closed" else "IN CORSO"
        out(
            f"#{i}  trade id={tr.get('id')}  signal id={tr.get('signal_id')}  "
            f"[{stato_label}]"
        )
        out("-" * 72)
        out(f"  Asset:        {tr.get('asset')}  ({tr.get('direction')})")
        out(f"  Aperto:       {tr.get('opened_at')}")
        if status == "closed":
            out(f"  Chiuso:       {tr.get('closed_at')}")
        out(f"  Size:         {_num(tr.get('size'))}")
        out(f"  Entry:        {_eur(tr.get('entry_price'))}")
        out(f"  Stop loss:    {_eur(tr.get('current_sl'))}")
        out(f"  Take profit:  {_eur(tr.get('current_tp'))}")
        if status == "closed":
            out(f"  Prezzo uscita:{_eur(tr.get('close_price'))}")
            out(f"  Motivo uscita:{tr.get('exit_reason') or 'n/d'}")
        out(
            f"  P&L:          {_eur_signed(tr.get('pnl'))}  "
            f"({_pct(tr.get('pnl_pct'))})"
        )

        # Dettagli del signal sorgente (tesi e contesto della decisione).
        out("")
        if sig:
            out("  -- Setup originario (signal) --")
            out(f"  Score:        {_num(sig.get('score'))}")
            out(f"  Costo atteso: {_eur(sig.get('expected_cost'))}")
            out(f"  Stato signal: {sig.get('status')}")
            out(f"  Creato:       {sig.get('created_at')}")
            thesis = (sig.get("thesis") or "").strip()
            if thesis:
                out("  Tesi:")
                for tline in thesis.splitlines():
                    out(f"    {tline}")
            feats = sig.get("features_at_decision")
            if feats:
                out("  Feature alla decisione:")
                if isinstance(feats, dict):
                    for k, v in feats.items():
                        out(f"    {k}: {v}")
                else:
                    out(f"    {feats}")
        else:
            out("  -- Setup originario non disponibile (signal_id assente) --")

        # Cronologia eventi di monitoraggio.
        out("")
        if events:
            out(f"  -- Eventi ({len(events)}) --")
            for ev in events:
                detail = ev.get("details")
                detail_str = ""
                if detail:
                    detail_str = "  " + json.dumps(detail, ensure_ascii=False)
                out(
                    f"    [{ev.get('created_at')}] {ev.get('event_type')}"
                    f"  {ev.get('reason') or ''}{detail_str}"
                )
        else:
            out("  -- Nessun evento di monitoraggio --")

    out("")
    out("=" * 72)
    out("RIEPILOGO (EURO)")
    out("=" * 72)
    out(f"  Setup chiusi:    {n_closed}   P&L realizzato:  {_eur_signed(realized)}")
    out(f"  Setup in corso:  {n_open}   P&L flottante:   {_eur_signed(floating)}")
    out(f"  Totale combinato:            {_eur_signed(realized + floating)}")
    return "\n".join(lines)


def render_json(setups: list[dict[str, Any]]) -> str:
    """Output JSON grezzo con metadati di valuta espliciti."""
    payload = {
        "currency": "EUR",
        "count": len(setups),
        "setups": setups,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export dettagliato dei setup (signal->trade) in EURO"
    )
    parser.add_argument(
        "--status",
        choices=["all", "open", "closed"],
        default="all",
        help="Filtra per stato del setup (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Esporta solo gli ultimi N setup (default: tutti dall'inizio)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON invece del formato leggibile",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Scrive su file invece che su stdout",
    )
    args = parser.parse_args()

    config = load_config()
    if not config.supabase_service_role_key:
        print(
            "ATTENZIONE: service_role key assente, uso anon key. "
            "Con RLS abilitato l'export risultera' vuoto.",
            file=sys.stderr,
        )
    db = Database(config)

    setups = fetch_setups(db, args.status, args.limit)
    rendered = render_json(setups) if args.json else render_text(setups)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"Export completato: {out_path}  ({len(setups)} setup, EURO)")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
