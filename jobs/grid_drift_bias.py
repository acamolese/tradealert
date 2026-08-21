"""Il grid simmetrico e' strutturalmente short sugli asset che salgono?

Ipotesi (2026-08-21, dopo 24h di perdite su grid short in rally): con
`target = -livello(prezzo)` e ancoraggio FISSO, un asset con deriva positiva
porta il prezzo stabilmente SOPRA l'ancoraggio, quindi il grid passa la maggior
parte del tempo SHORT e accumula proprio quando il mercato sale. Non sarebbe
sfortuna ma un difetto di progetto: su un asset che sale, un grid ancorato a un
punto fisso e' short-biased per costruzione.

Predizione se VERA: su storico lungo la quota di tempo passata short cresce con
la deriva dell'asset, e il P&L del grid e' negativamente correlato alla deriva.
Predizione se FALSA: tempo long/short ~50/50 indipendentemente dalla deriva.

Sola lettura, nessun ordine.
"""
from __future__ import annotations

import sys

from src.config import load_config
from src.capital_client import CapitalClient
from src.grid_net import target_unita


def analizza(closes: list[float], step: float, max_unita: int) -> dict:
    """Percentuale di tempo long/short/flat e unita' medie, ancoraggio fisso."""
    p0 = closes[0]
    long_t = short_t = flat_t = 0
    somma = 0.0
    for p in closes:
        t = target_unita(p, p0, step, max_unita)
        somma += t
        if t > 0:
            long_t += 1
        elif t < 0:
            short_t += 1
        else:
            flat_t += 1
    n = len(closes)
    return {"long": long_t / n * 100, "short": short_t / n * 100,
            "flat": flat_t / n * 100, "media": somma / n,
            "deriva": (closes[-1] / closes[0] - 1) * 100}


def main() -> int:
    capital = CapitalClient(load_config())
    capital.login()
    epics = sys.argv[1:] or ["BTCUSD", "GOLD", "US100", "DE40", "US500", "ETHUSD"]
    print(f"{'epic':<9} {'deriva':>9} {'%long':>7} {'%short':>7} {'%flat':>6} "
          f"{'unita media':>12}")
    righe = []
    for ep in epics:
        try:
            closes = []
            for x in capital.get_prices(ep, resolution="DAY", max_bars=400):
                cp = x.get("closePrice") or {}
                if cp.get("bid") and cp.get("ask"):
                    closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
            if len(closes) < 100:
                continue
            r = analizza(closes, 0.002, 10)
            righe.append((r["deriva"], ep, r))
            print(f"{ep:<9} {r['deriva']:>+8.1f}% {r['long']:>6.1f}% {r['short']:>6.1f}% "
                  f"{r['flat']:>5.1f}% {r['media']:>+11.2f}")
        except Exception as exc:
            print(f"{ep:<9} errore: {str(exc)[:40]}")

    if len(righe) >= 3:
        righe.sort()
        print("\nCORRELAZIONE deriva -> unita' medie:")
        d = [x[0] for x in righe]
        u = [x[2]["media"] for x in righe]
        md, mu = sum(d) / len(d), sum(u) / len(u)
        num = sum((a - md) * (b - mu) for a, b in zip(d, u))
        den = (sum((a - md) ** 2 for a in d) * sum((b - mu) ** 2 for b in u)) ** 0.5
        corr = num / den if den else 0
        print(f"  r = {corr:+.3f}")
        print(f"  {'CONFERMATA' if corr < -0.5 else 'NON confermata'}: "
              f"{'piu' if corr < -0.5 else 'la deriva non spiega'} "
              f"{'sale un asset, piu il grid ci sta short contro' if corr < -0.5 else 'la posizione media'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
