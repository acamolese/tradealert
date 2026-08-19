"""Quali strumenti reggono il grid? Sweep multi-mercato.

Richiesta "maggiore varieta'": invece di aggiungere strumenti a intuito, si
misurano. Criterio: percentuale di finestre mobili positive (250 giorni, hop 50)
su 1000 barre giornaliere, con drawdown e ritmo di trade come contorno.

Nota sui dati: le barre DAY sottostimano gli attraversamenti dei passi stretti,
quindi il ritmo reale e' piu' alto di quello stampato (su BTCUSD ~4.4x).
"""
from __future__ import annotations

import sys

from src.config import load_config
from src.capital_client import CapitalClient
from src.spinner_odds import annual_financing
from src.executor import _market_meta
from src.risk import quote_to_ref_factor
from jobs.grid_backtest import simula, finestre

EPICS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LTCUSD", "DOGEUSD", "ADAUSD",
         "LINKUSD", "BCHUSD", "XLMUSD", "GOLD", "SILVER", "US100", "US500",
         "DE40", "OIL_CRUDE", "NATURALGAS"]
EQUITY = 200.0


def main() -> int:
    capital = CapitalClient(load_config())
    capital.login()
    lev = capital.get_leverages_map()
    ris = []
    for ep in EPICS:
        try:
            mk = capital.get_market(ep)
            instr = mk.get("instrument", {}) or {}
            snap = mk.get("snapshot", {}) or {}
            meta = _market_meta(mk, leverages_map=lev, use_real_leverage=True)
            bid, offer = float(snap["bid"]), float(snap["offer"])
            mid = (bid + offer) / 2
            spread = (offer - bid) / mid
            q2r = quote_to_ref_factor(instr.get("currency"), capital) or 1.0
            unit = meta["min_size"] * mid * q2r
            of = instr.get("overnightFee") or {}
            iv = of.get("swapChargeInterval")
            fin = {"long": annual_financing(of.get("longRate"), iv),
                   "short": annual_financing(of.get("shortRate"), iv)}
            closes = []
            for x in capital.get_prices(ep, resolution="DAY", max_bars=1000):
                cp = x.get("closePrice") or {}
                if cp.get("bid") and cp.get("ask"):
                    closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
            if len(closes) < 300:
                continue
            for side in ("long", "short"):
                for step in (0.01, 0.02, 0.03):
                    r = finestre(closes, step, spread, fin[side], EQUITY, unit,
                                 10, side, 1, 250, 50)
                    if not r:
                        continue
                    pnl = [x[0] for x in r]
                    dds = [x[1] for x in r]
                    pos = sum(1 for x in pnl if x > 0) / len(pnl)
                    med = sorted(pnl)[len(pnl) // 2]
                    _, tr, _, _, _, _ = simula(closes, step, spread, fin[side],
                                               EQUITY, unit, 10, side, 1)
                    # VINCOLO DI SOPRAVVIVENZA: il simulatore non conosce la
                    # chiamata di margine, quindi una configurazione con DD
                    # superiore all'equity e' fantasia (il conto sarebbe gia'
                    # chiuso). Si scarta invece di mostrarla in cima.
                    if max(dds) >= EQUITY * 0.5:
                        continue
                    ris.append((pos, med, ep, side, step, max(dds),
                                tr / len(closes), unit, fin[side], min(pnl)))
        except Exception as exc:
            print(f"{ep:<11} errore: {str(exc)[:50]}")

    ris.sort(key=lambda x: (-x[0], -x[1]))
    print(f"\n(scartate le configurazioni con drawdown >= {EQUITY*0.5:.0f}€, "
          f"meta' dell'equity: il conto non sopravviverebbe)")
    print(f"\n{'epic':<11} {'side':<6} {'passo':>5} {'%pos':>5} {'mediana':>9} "
          f"{'peggio':>8} {'DDmax':>7} {'trade/gg':>8} {'unita':>8} {'fin':>7}")
    for pos, med, ep, side, step, dd, tg, unit, fin, peggio in ris[:25]:
        print(f"{ep:<11} {side:<6} {step*100:>4.0f}% {pos*100:>4.0f}% {med:>+8.1f}€ "
              f"{peggio:>+7.1f}€ {dd:>6.1f}€ {tg:>8.2f} {unit:>7.2f}€ {fin*100:>+6.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
