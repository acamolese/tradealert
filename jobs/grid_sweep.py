"""Sweep parametrico del grid: quale configurazione regge, non quale rende di piu'.

Il criterio NON e' il rendimento massimo del singolo path (fortuna del punto di
arrivo) ma la percentuale di finestre mobili positive, con il drawdown come
vincolo. Serve a dimensionare la variante demo richiesta il 2026-08-18.

ATTENZIONE ai dati: su barre DAY un passo stretto (0.5-1%) viene attraversato
molte volte al giorno nella realta' ma il backtest ne vede al massimo uno per
barra, quindi SOTTOSTIMA sia i giri sia i costi dei passi stretti. Per quelli il
riferimento e' la risoluzione HOUR, che pero' copre solo ~41 giorni.

Uso: python -m jobs.grid_sweep [epic] [resolution] [equity] [budget_margine]
"""
from __future__ import annotations

import sys

from src.config import load_config
from src.capital_client import CapitalClient
from src.spinner_odds import annual_financing
from jobs.grid_backtest import simula, finestre


def main() -> int:
    epic = sys.argv[1] if len(sys.argv) > 1 else "BTCUSD"
    res = sys.argv[2] if len(sys.argv) > 2 else "DAY"
    equity = float(sys.argv[3]) if len(sys.argv) > 3 else 200.0
    budget = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0

    capital = CapitalClient(load_config())
    capital.login()
    from src.executor import _market_meta
    from src.risk import quote_to_ref_factor
    mk = capital.get_market(epic)
    instr, snap = mk.get("instrument", {}) or {}, mk.get("snapshot", {}) or {}
    meta = _market_meta(mk, leverages_map=capital.get_leverages_map(),
                        use_real_leverage=True)
    bid, offer = float(snap["bid"]), float(snap["offer"])
    mid = (bid + offer) / 2
    spread = (offer - bid) / mid
    q2r = quote_to_ref_factor(instr.get("currency"), capital) or 1.0
    unit = meta["min_size"] * mid * q2r                 # nozionale di una unita'
    margine_unit = unit * meta["margin_factor"]
    of = instr.get("overnightFee") or {}
    iv = of.get("swapChargeInterval")
    fin = {"long": annual_financing(of.get("longRate"), iv),
           "short": annual_financing(of.get("shortRate"), iv)}

    closes = []
    for x in capital.get_prices(epic, resolution=res, max_bars=1000):
        cp = x.get("closePrice") or {}
        if cp.get("bid") and cp.get("ask"):
            closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
    bpd = {"DAY": 1, "HOUR": 24, "MINUTE_15": 96}[res]
    giorni = len(closes) / bpd
    max_unita = int(budget / margine_unit) if margine_unit > 0 else 0

    print(f"{epic} {res}: {len(closes)} barre = {giorni:.0f} giorni | "
          f"da {closes[0]:.0f} a {closes[-1]:.0f} ({(closes[-1]/closes[0]-1)*100:+.0f}%)")
    print(f"unita' = {unit:.2f}€ nozionale / {margine_unit:.2f}€ margine | "
          f"budget {budget:.0f}€ -> max {max_unita} unita' | equity {equity:.0f}€")
    print(f"spread {spread*10000:.1f}bp | fin long {fin['long']*100:+.1f}% "
          f"short {fin['short']*100:+.1f}%\n")

    win = int(250 * bpd) if giorni > 300 else int(giorni * bpd / 3)
    hop = max(1, win // 5)
    print(f"finestre da {win/bpd:.0f} giorni, hop {hop/bpd:.0f} giorni\n")
    print(f"{'side':<6} {'passo':>6} {'unita':>6} {'%pos':>6} {'mediana':>9} "
          f"{'peggiore':>9} {'DDmax':>7} {'trade/gg':>9}  copertura")
    righe = []
    for side in ("long", "short"):
        for step in (0.005, 0.01, 0.02, 0.03, 0.05):
            for nlv in sorted({10, 20, max_unita}):
                if nlv <= 0 or nlv > max_unita:
                    continue
                r = finestre(closes, step, spread, fin[side], equity, unit,
                             nlv, side, bpd, win, hop)
                if not r:
                    continue
                pnl = [x[0] for x in r]
                dds = [x[1] for x in r]
                pos = sum(1 for p in pnl if p > 0)
                med = sorted(pnl)[len(pnl) // 2]
                _, tr, _, _, _, _ = simula(closes, step, spread, fin[side],
                                           equity, unit, nlv, side, bpd)
                cop = (1 - (1 - step) ** nlv) * 100 if side == "long" else \
                      ((1 + step) ** nlv - 1) * 100
                righe.append((pos / len(pnl), med, side, step, nlv, min(pnl),
                              max(dds), tr / giorni, cop))
                print(f"{side:<6} {step*100:>5.1f}% {nlv:>6} {pos/len(pnl)*100:>5.0f}% "
                      f"{med:>+8.1f}€ {min(pnl):>+8.1f}€ {max(dds):>6.1f}€ "
                      f"{tr/giorni:>9.2f}  -{cop:.0f}%")
    if righe:
        righe.sort(key=lambda r: (-r[0], -r[1]))
        b = righe[0]
        print(f"\nMIGLIORE per robustezza: {b[2]} passo {b[3]*100:.1f}% "
              f"{b[4]} unita' -> {b[0]*100:.0f}% finestre positive, "
              f"mediana {b[1]:+.1f}€, DD max {b[6]:.1f}€, {b[7]:.2f} trade/giorno")
    return 0


if __name__ == "__main__":
    sys.exit(main())
