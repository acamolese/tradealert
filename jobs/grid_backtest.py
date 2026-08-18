"""Backtest ONESTO del grid: con inventory, capitale finito e costi veri.

`grid_feasibility` conta le oscillazioni e da' numeri entusiasmanti (3%/giorno):
quel calcolo assume inventory infinito e che il prezzo torni SEMPRE. Il grid pero'
e' short volatilita' di lungo periodo: incassa a gocce in laterale e accumula
perdite in trend, perche' continua a comprare mentre il prezzo scende.

Qui si simula il meccanismo vero:
- griglia geometrica a passo P attorno al prezzo iniziale;
- a ogni discesa di un livello si APRE una unita' (long) o si chiude una short;
- a ogni risalita si CHIUDE con profitto P lordo, meno spread;
- il capitale e' finito: oltre MAX_LIVELLI non si compra piu' (e li' iniziano le
  perdite non compensate);
- il financing si paga ogni notte su TUTTO l'inventory aperto.

Il verdetto e' l'equity finale, non il numero di trade.

Uso: python -m jobs.grid_backtest [epic] [resolution] [passo%]
"""
from __future__ import annotations

import math
import sys

from src.config import load_config
from src.capital_client import CapitalClient
from src.spinner_odds import annual_financing


def simula(closes, step, spread, fin_annual, equity0, unit_notional,
           max_livelli, side="long", bars_per_day=24):
    """Ritorna (equity, trade, max_inventory, drawdown_max, incassi, costi)."""
    equity = equity0
    inventory: list[float] = []          # prezzi di carico delle unita' aperte
    trade = 0
    incassi = costi = 0.0
    peak = equity
    dd = 0.0
    max_inv = 0
    lvl = lambda p: math.floor(math.log(p) / math.log(1 + step))
    cur = lvl(closes[0])
    fin_per_bar = fin_annual / 365.0 / bars_per_day

    for i, p in enumerate(closes[1:], 1):
        new = lvl(p)
        while new < cur:                 # il prezzo scende di un livello
            cur -= 1
            if side == "long" and len(inventory) < max_livelli:
                inventory.append(p)
                trade += 1
                c = unit_notional * spread / 2
                equity -= c
                costi += c
            elif side == "short" and inventory:
                carico = inventory.pop()
                g = unit_notional * step
                c = unit_notional * spread / 2
                equity += g - c
                incassi += g
                costi += c
                trade += 1
        while new > cur:                 # il prezzo sale di un livello
            cur += 1
            if side == "long" and inventory:
                inventory.pop()
                g = unit_notional * step
                c = unit_notional * spread / 2
                equity += g - c
                incassi += g
                costi += c
                trade += 1
            elif side == "short" and len(inventory) < max_livelli:
                inventory.append(p)
                trade += 1
                c = unit_notional * spread / 2
                equity -= c
                costi += c
        # financing su tutto l'inventory aperto
        if inventory:
            f = len(inventory) * unit_notional * fin_per_bar
            equity -= f
            costi += f
        max_inv = max(max_inv, len(inventory))
        # equity mark-to-market: l'inventory aperto vale al prezzo corrente
        mtm = sum((p - c0) if side == "long" else (c0 - p) for c0 in inventory)
        mtm = mtm / closes[0] * unit_notional * len(inventory) / max(len(inventory), 1)
        eq_mtm = equity + sum(((p - c0) if side == "long" else (c0 - p)) / c0
                              * unit_notional for c0 in inventory)
        peak = max(peak, eq_mtm)
        dd = max(dd, peak - eq_mtm)
    p = closes[-1]
    eq_finale = equity + sum(((p - c0) if side == "long" else (c0 - p)) / c0
                             * unit_notional for c0 in inventory)
    return eq_finale, trade, max_inv, dd, incassi, costi


def main() -> int:
    epic = sys.argv[1] if len(sys.argv) > 1 else "BTCUSD"
    res = sys.argv[2] if len(sys.argv) > 2 else "DAY"
    capital = CapitalClient(load_config())
    capital.login()
    mk = capital.get_market(epic)
    instr, snap = mk.get("instrument", {}) or {}, mk.get("snapshot", {}) or {}
    bid, offer = float(snap.get("bid") or 0), float(snap.get("offer") or 0)
    mid = (bid + offer) / 2
    spread = (offer - bid) / mid
    of = instr.get("overnightFee") or {}
    iv = of.get("swapChargeInterval")
    fin = {"long": annual_financing(of.get("longRate"), iv),
           "short": annual_financing(of.get("shortRate"), iv)}

    closes = []
    for x in capital.get_prices(epic, resolution=res, max_bars=1000):
        cp = x.get("closePrice") or {}
        if cp.get("bid") and cp.get("ask"):
            closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
    bpd = {"DAY": 1, "HOUR": 24, "MINUTE_15": 96}.get(res, 1)
    giorni = len(closes) / bpd
    print(f"{epic} {res}: {len(closes)} barre = {giorni:.0f} giorni")
    print(f"  da {closes[0]:.2f} a {closes[-1]:.2f} ({(closes[-1]/closes[0]-1)*100:+.1f}%) "
          f"| min {min(closes):.2f} max {max(closes):.2f}")
    print(f"  spread {spread*10000:.1f}bp | fin long {fin['long']*100:+.1f}%/anno "
          f"short {fin['short']*100:+.1f}%/anno")
    print(f"  buy&hold long: {(closes[-1]/closes[0]-1)*100:+.1f}%\n")

    EQ, UNIT, MAXLV = 100.0, 6.5, 10      # equity demo, taglia min BTCUSD, livelli
    print(f"  equity {EQ:.0f}€ | unita' {UNIT}€ | max {MAXLV} livelli "
          f"(nozionale max {UNIT*MAXLV:.0f}€)\n")
    print(f"{'side':<6} {'passo':>6} {'equity fin':>11} {'P&L':>8} {'trade':>7} "
          f"{'inv.max':>8} {'DD':>7} {'incassi':>8} {'costi':>8}")
    for side in ("long", "short"):
        for step in (0.01, 0.02, 0.03, 0.05, 0.08):
            eq, tr, mi, dd, inc, cos = simula(
                closes, step, spread, fin[side], EQ, UNIT, MAXLV, side, bpd)
            print(f"{side:<6} {step*100:>5.0f}% {eq:>11.2f} {eq-EQ:>+8.2f} {tr:>7} "
                  f"{mi:>8} {dd:>7.2f} {inc:>8.2f} {cos:>8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def finestre(closes, step, spread, fin, eq0, unit, maxlv, side, bpd, win, hop):
    """Distribuzione del risultato su finestre mobili: un solo path puo' essere
    fortunato (il grid short 'vince' se il periodo finisce dopo una discesa).
    Se l'edge esiste, deve reggere sulla maggioranza delle finestre."""
    out = []
    for start in range(0, max(1, len(closes) - win), hop):
        seg = closes[start:start + win]
        if len(seg) < win * 0.9:
            break
        eq, tr, mi, dd, inc, cos = simula(seg, step, spread, fin, eq0, unit,
                                          maxlv, side, bpd)
        out.append((eq - eq0, dd, (seg[-1] / seg[0] - 1) * 100))
    return out
