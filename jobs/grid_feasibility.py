"""Si puo' guadagnare dal MOVIMENTO invece che dalla direzione?

Richiesta 2026-08-18: "vorrei qualcosa che si muove molto". La domanda seria
sotto la richiesta e': esiste una strategia che monetizza l'OSCILLAZIONE, senza
prevedere la direzione? E' il grid trading: si mettono ordini a scaletta, ogni
volta che il prezzo attraversa un livello si compra/vende, e ogni andata+ritorno
incassa il passo della griglia meno i costi.

Il grid e' l'unica famiglia che il progetto non ha ancora falsificato, ed e'
strutturalmente diversa da tutto cio' che e' stato bocciato: non predice nulla,
guadagna dalla varianza, e si muove per costruzione (e' cio' che l'utente chiede).

Questo script NON implementa il grid: misura se ha margine. Conta gli
attraversamenti reali di una griglia a passo P su storico vero, li moltiplica per
il guadagno lordo per attraversamento e sottrae spread e financing. Se il netto e'
negativo, il grid e' solo un modo elegante di regalare spread al broker.

Uso: python -m jobs.grid_feasibility [epic] [giorni]
"""
from __future__ import annotations

import sys

from src.config import load_config
from src.capital_client import CapitalClient
from src.spinner_odds import annual_financing


def crossings(prices: list[float], step_pct: float) -> int:
    """Attraversamenti di una griglia geometrica a passo step_pct.

    Si conta il numero di volte che il prezzo passa da un livello all'altro: e'
    la quantita' di trade che la griglia genererebbe. Ogni coppia di
    attraversamenti in direzioni opposte e' un giro completo (un incasso).
    """
    if not prices or step_pct <= 0:
        return 0
    import math
    lvl = lambda p: math.floor(math.log(p) / math.log(1 + step_pct))
    cur = lvl(prices[0])
    n = 0
    for p in prices[1:]:
        new = lvl(p)
        if new != cur:
            n += abs(new - cur)
            cur = new
    return n


def main() -> int:
    epic = sys.argv[1] if len(sys.argv) > 1 else "BTCUSD"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    capital = CapitalClient(load_config())
    capital.login()

    mk = capital.get_market(epic)
    instr = mk.get("instrument", {}) or {}
    snap = mk.get("snapshot", {}) or {}
    bid, offer = float(snap.get("bid") or 0), float(snap.get("offer") or 0)
    mid = (bid + offer) / 2 if bid and offer else 0
    spread_bps = ((offer - bid) / mid * 10000) if mid else 0
    of = instr.get("overnightFee") or {}
    iv = of.get("swapChargeInterval")
    fin_long = annual_financing(of.get("longRate"), iv)
    fin_short = annual_financing(of.get("shortRate"), iv)

    # storico piu' fitto disponibile: la granularita' decide quante oscillazioni
    # si vedono davvero (su barre giornaliere il micro-movimento e' invisibile)
    bars = []
    for res, cap in (("MINUTE_15", 1000), ("HOUR", 1000)):
        try:
            p = capital.get_prices(epic, resolution=res, max_bars=cap)
            if p:
                bars = [(x, res) for x in p]
                break
        except Exception:
            continue
    if not bars:
        print("nessuno storico")
        return 1
    res = bars[0][1]
    closes = []
    for x, _ in bars:
        cp = x.get("closePrice") or {}
        if cp.get("bid") and cp.get("ask"):
            closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
    if len(closes) < 50:
        print("storico troppo corto")
        return 1

    per_bar = {"MINUTE_15": 96, "HOUR": 24}[res]
    giorni_reali = len(closes) / per_bar
    print(f"{epic}: {len(closes)} barre {res} = {giorni_reali:.1f} giorni")
    print(f"  prezzo {mid:.2f} | spread {spread_bps:.1f}bp | "
          f"financing long {fin_long*100:+.2f}%/anno short {fin_short*100:+.2f}%/anno")
    print(f"  escursione periodo: min {min(closes):.2f} max {max(closes):.2f} "
          f"({(max(closes)/min(closes)-1)*100:+.1f}%)")
    print(f"  primo {closes[0]:.2f} -> ultimo {closes[-1]:.2f} "
          f"({(closes[-1]/closes[0]-1)*100:+.1f}% di deriva)\n")

    print(f"{'passo':>7} {'attravers.':>11} {'giri/gg':>8} {'lordo/giro':>11} "
          f"{'costo/giro':>11} {'netto/giro':>11} {'netto/gg %':>11}")
    for step in (0.002, 0.005, 0.01, 0.02, 0.03, 0.05):
        n = crossings(closes, step)
        giri = n / 2.0
        giri_gg = giri / giorni_reali if giorni_reali else 0
        lordo = step                      # un giro completo incassa il passo
        costo = spread_bps / 10000.0      # spread pagato sul giro
        netto = lordo - costo
        # rendimento giornaliero sul nozionale IMPEGNATO in un livello
        netto_gg = netto * giri_gg * 100
        print(f"{step*100:>6.1f}% {n:>11} {giri_gg:>8.2f} {lordo*100:>10.2f}% "
              f"{costo*100:>10.2f}% {netto*100:>10.2f}% {netto_gg:>10.3f}%")

    print("\nNOTE:")
    print("  - 'netto/gg %' e' sul nozionale di UN livello, non sull'equity")
    print("  - non include il financing: va aggiunto (o sottratto) a parte")
    print("  - assume che il prezzo TORNI: la deriva non compensata e' la perdita")
    print("    latente del grid (inventory risk), qui visibile come '% di deriva'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
