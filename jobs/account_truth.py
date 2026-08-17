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
from src.capital_client import CapitalClient


def fetch_transactions(capital, days: int) -> list[dict]:
    """Storico transazioni, paginato a ritroso: l'endpoint tronca a 100 righe,
    quindi si spezza la finestra in blocchi finche' non si esaurisce il periodo."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out: list[dict] = []
    seen: set[str] = set()
    cur_end = end
    while cur_end > start:
        cur_start = max(start, cur_end - timedelta(days=7))
        url = (f"/history/transactions?from={cur_start.strftime('%Y-%m-%dT%H:%M:%S')}"
               f"&to={cur_end.strftime('%Y-%m-%dT%H:%M:%S')}")
        r = capital._session.get(capital._url(url),
                                 headers=capital._auth_headers(), timeout=20)
        if r.status_code != 200:
            print(f"  ! finestra {cur_start.date()}..{cur_end.date()}: HTTP {r.status_code}")
            break
        tx = r.json().get("transactions", [])
        for t in tx:
            ref = t.get("reference") or f"{t.get('date')}|{t.get('size')}|{t.get('note')}"
            if ref not in seen:
                seen.add(ref)
                out.append(t)
        if len(tx) >= 100:
            print(f"  ! finestra {cur_start.date()}..{cur_end.date()} satura "
                  f"(100 righe): possibile troncamento")
        cur_end = cur_start
    out.sort(key=lambda t: t.get("dateUtc") or t.get("date") or "")
    return out


def _amount(t: dict) -> float:
    try:
        return float(t.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    capital = CapitalClient(load_config())
    capital.login()

    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    equity = float(bal.get("balance") or 0) + float(bal.get("profitLoss") or 0)
    print(f"CONTO: saldo {bal.get('balance')}€ | flottante {bal.get('profitLoss')}€ "
          f"| equity {equity:.2f}€ | depositato {bal.get('deposit')}€\n")

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
