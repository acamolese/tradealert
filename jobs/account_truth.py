"""Verita' economica del conto, letta dal BROKER (non dal DB).

Nasce da due esigenze:
1. i pnl in euro nel DB sono inaffidabili (segno invertito, memoria `pnl_db_bug`),
   quindi ogni misura di resa deve partire da /history/transactions;
2. il modello di costo dello spinner e' dichiarato "in verifica" (§12.8) e va
   confrontato con quello che il broker ADDEBITA davvero.

Scompone il conto in: P&L dei trade, financing (SWAP), dividendi
(CORPORATE_ACTION), depositi/prelievi. Da qui la resa settimanale oggettiva.

Uso: python -m jobs.account_truth [giorni]
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.capital_client import (CapitalClient, cash_conto, equity_conto,
                                flottante_conto)


def fetch_transactions(capital, days: int) -> list[dict]:
    """Storico transazioni, paginato a ritroso.

    L'endpoint tronca a 100 righe per chiamata. Con i grid, che fanno decine di
    movimenti al giorno, una finestra di 7 giorni satura e i movimenti piu'
    vecchi spariscono: il 21/08 il riepilogo mostrava "+0.49€ oggi" mentre il
    conto aveva perso 5.66€ realizzati. Ora la finestra si RESTRINGE finche' non
    rientra sotto il limite, invece di limitarsi ad avvisare.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out: list[dict] = []
    seen: set[str] = set()
    cur_end = end
    ampiezza = timedelta(days=min(7, max(1, days)))
    while cur_end > start:
        cur_start = max(start, cur_end - ampiezza)
        url = (f"/history/transactions?from={cur_start.strftime('%Y-%m-%dT%H:%M:%S')}"
               f"&to={cur_end.strftime('%Y-%m-%dT%H:%M:%S')}")
        r = capital._session.get(capital._url(url),
                                 headers=capital._auth_headers(), timeout=20)
        if r.status_code != 200:
            print(f"  ! finestra {cur_start.date()}..{cur_end.date()}: HTTP {r.status_code}")
            break
        tx = r.json().get("transactions", [])
        # finestra satura: dimezza e riprova, cosi' nessun movimento va perso
        if len(tx) >= 100 and ampiezza > timedelta(hours=1):
            ampiezza = max(timedelta(hours=1), ampiezza / 2)
            continue
        for t in tx:
            ref = t.get("reference") or f"{t.get('date')}|{t.get('size')}|{t.get('note')}"
            if ref not in seen:
                seen.add(ref)
                out.append(t)
        cur_end = cur_start
    out.sort(key=lambda t: t.get("dateUtc") or t.get("date") or "")
    return out


def _amount(t: dict) -> float:
    try:
        return float(t.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


def weekly_snapshot(capital, db, telegram=None) -> dict:
    """Resa/perdita OGGETTIVA della settimana, letta dal broker.

    Misura richiesta esplicitamente il 2026-08-17. Fonte = /history/transactions +
    saldo: mai il DB, i cui pnl hanno segno inaffidabile. Scompone il risultato in
    trade / financing / dividendi, cosi' si vede SEMPRE quanto e' costata la sola
    detenzione, che e' il numero che decide se il conto puo' capitalizzare.

    Persiste in monitoring_events (`account_week`) per costruire la serie storica:
    dalla seconda settimana il confronto e' con lo snapshot precedente, quindi la
    variazione di equity e' esatta e non stimata.
    """
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    equity = equity_conto(bal)

    tx = fetch_transactions(capital, 8)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week = [t for t in tx if (t.get("dateUtc") or "") >= cutoff]
    per_type: dict[str, float] = defaultdict(float)
    for t in week:
        per_type[t.get("transactionType") or "?"] += _amount(t)
    realizzato = sum(per_type.values())

    prev = None
    try:
        rows = (db._client.table("monitoring_events").select("details,created_at")
                .eq("event_type", "account_week")
                .order("created_at", desc=True).limit(1).execute().data)
        if rows:
            prev = rows[0].get("details") or {}
    except Exception:
        pass

    # Confronto con lo snapshot precedente sul campo "balance": fino al 01/09 il
    # campo "equity" era gonfiato dal doppio conteggio del flottante (vedi
    # equity_conto), mentre "balance" conteneva gia' l'equity vera del broker.
    # Leggere "balance" tiene la serie storica omogenea attraverso il fix.
    prev_eq = float(prev.get("balance") or prev.get("equity") or 0) if prev else 0.0
    delta_equity = (equity - prev_eq) if prev_eq else None
    pct = (delta_equity / prev_eq * 100) if (delta_equity is not None and prev_eq) else None

    pos = capital.get_open_positions()
    snap = {
        "equity": round(equity, 2),
        # "balance" = equity, ridondante ma tenuto perche' e' il campo su cui si
        # aggancia la serie storica pre-fix; il cash sta in "cash"
        "balance": round(equity, 2),
        "cash": round(cash_conto(bal), 2),
        "floating": round(flottante_conto(bal), 2),
        "trade": round(per_type.get("TRADE", 0.0), 2),
        "financing": round(per_type.get("SWAP", 0.0), 2),
        "dividendi": round(per_type.get("CORPORATE_ACTION", 0.0), 2),
        "realizzato": round(realizzato, 2),
        "delta_equity": round(delta_equity, 2) if delta_equity is not None else None,
        "pct": round(pct, 2) if pct is not None else None,
        "posizioni": len(pos),
        "movimenti": len(week),
    }

    if telegram:
        seg = "n/d (prima settimana)" if snap["delta_equity"] is None else \
              f"{snap['delta_equity']:+.2f}€ ({snap['pct']:+.2f}%)"
        costo_anno = snap["financing"] / 7 * 365
        telegram.send_message(
            f"📊 <b>Settimana — resa oggettiva</b>\n"
            f"Equity: <b>{snap['equity']:.2f}€</b> (cash {snap['cash']:.2f} + "
            f"flottante {snap['floating']:+.2f})\n"
            f"Variazione: <b>{seg}</b>\n\n"
            f"Da cosa viene:\n"
            f"• trade chiusi {snap['trade']:+.2f}€\n"
            f"• interessi overnight <b>{snap['financing']:+.2f}€</b>\n"
            f"• dividendi {snap['dividendi']:+.2f}€\n"
            f"• movimenti: {snap['movimenti']} | posizioni aperte: {snap['posizioni']}\n\n"
            f"<i>Al ritmo attuale la sola detenzione costa {costo_anno:.2f}€/anno "
            f"({abs(costo_anno)/snap['equity']*100:.1f}% dell'equity).</i>"
        )
    try:
        db._client.table("monitoring_events").insert(
            {"event_type": "account_week", "details": snap}).execute()
    except Exception:
        pass
    return snap


def main() -> int:
    if "--weekly" in sys.argv:
        from src.db import Database
        from src.telegram_client import TelegramClient
        v1 = load_config()
        capital = CapitalClient(v1)
        capital.login()
        snap = weekly_snapshot(capital, Database(v1),
                               None if "--no-telegram" in sys.argv else TelegramClient(v1))
        for k, v in snap.items():
            print(f"  {k}: {v}")
        return 0

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    capital = CapitalClient(load_config())
    capital.login()

    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    equity = equity_conto(bal)
    print(f"CONTO: equity {equity:.2f}€ (cash {bal.get('deposit')}€ "
          f"+ flottante {bal.get('profitLoss')}€) | disponibile {bal.get('available')}€\n")

    tx = fetch_transactions(capital, days)
    if not tx:
        print("Nessuna transazione.")
        return 1
    first = (tx[0].get("dateUtc") or "")[:10]
    last = (tx[-1].get("dateUtc") or "")[:10]
    print(f"TRANSAZIONI: {len(tx)} dal {first} al {last}")
    print(f"  {Counter(t.get('transactionType') for t in tx)}\n")

    per_type: dict[str, float] = defaultdict(float)
    per_note: dict[str, float] = defaultdict(float)
    for t in tx:
        per_type[t.get("transactionType") or "?"] += _amount(t)
        per_note[f"{t.get('transactionType')}/{t.get('note')}"] += _amount(t)

    print("BILANCIO PER TIPO (EUR):")
    for k, v in sorted(per_type.items(), key=lambda x: x[1]):
        print(f"  {k:<18} {v:>9.2f}")
    print(f"  {'TOTALE':<18} {sum(per_type.values()):>9.2f}\n")

    print("DETTAGLIO PER CAUSALE (EUR):")
    for k, v in sorted(per_note.items(), key=lambda x: x[1]):
        n = sum(1 for t in tx if f"{t.get('transactionType')}/{t.get('note')}" == k)
        print(f"  {k:<34} {v:>8.2f}  ({n} mov.)")

    # --- costo di detenzione annualizzato, il numero che decide se si capitalizza ---
    swaps = [t for t in tx if t.get("transactionType") == "SWAP"]
    if swaps:
        giorni = max(1, (datetime.fromisoformat(swaps[-1]["dateUtc"])
                         - datetime.fromisoformat(swaps[0]["dateUtc"])).days)
        tot_swap = sum(_amount(t) for t in swaps)
        print(f"\nFINANCING: {tot_swap:.2f}€ in {giorni} giorni "
              f"({tot_swap/giorni:.4f}€/giorno)")
        pos = capital.get_open_positions()
        nozionale = 0.0
        for p in pos:
            po, mk = p.get("position", {}), p.get("market", {})
            lvl = po.get("level") or mk.get("bid") or 0
            nozionale += float(po.get("size") or 0) * float(lvl or 0)
        print(f"  posizioni aperte: {len(pos)} | nozionale ~{nozionale:.2f} "
              f"(valuta dello strumento)")
        if nozionale > 0:
            annuo = (tot_swap / giorni) * 365
            print(f"  costo annualizzato {annuo:.2f}€ su ~{nozionale:.0f} di nozionale "
                  f"= {abs(annuo)/nozionale*100:.2f}%/anno")
            print(f"  (il tabellone modella 1-2%/anno: se qui esce molto di piu', il"
                  f" modello di costo sottostima e il premio atteso e' illusorio)")

    # --- settimane: la resa oggettiva richiesta ---
    print("\nPER SETTIMANA (EUR, dal broker):")
    per_week: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in tx:
        d = (t.get("dateUtc") or t.get("date") or "")[:10]
        if not d:
            continue
        wk = datetime.fromisoformat(d).strftime("%G-W%V")
        per_week[wk][t.get("transactionType") or "?"] += _amount(t)
    print(f"  {'settimana':<10} {'TRADE':>8} {'SWAP':>8} {'DIVID.':>8} {'NETTO':>8}")
    for wk in sorted(per_week):
        w = per_week[wk]
        netto = sum(w.values())
        print(f"  {wk:<10} {w.get('TRADE', 0):>8.2f} {w.get('SWAP', 0):>8.2f} "
              f"{w.get('CORPORATE_ACTION', 0):>8.2f} {netto:>8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
