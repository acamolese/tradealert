"""L'ancoraggio mobile risolve il bias short? Confronto misurato.

Verifica del fix 2026-08-21. Confronta ancoraggio FISSO (com'era, che ha prodotto
-5.43€ sul conto reale in 24h) contro ancoraggio MOBILE a media esponenziale, su
storico lungo e su piu' strumenti.

Criterio: il bias e' risolto se il tempo passato al tetto della posizione crolla
e la correlazione tra deriva dell'asset e posizione media si azzera.
"""
from __future__ import annotations

import sys

from src.config import load_config
from src.capital_client import CapitalClient
from src.grid_net import target_unita, ancora_mobile


def simula(closes, step, max_unita, spread, periodo_ema=None):
    """P&L del grid con ancoraggio fisso (periodo_ema=None) o mobile."""
    pos = 0.0
    contante = 0.0
    al_tetto = 0
    somma_pos = 0.0
    minimo = 30 if periodo_ema else 1
    for i in range(minimo, len(closes)):
        p = closes[i]
        if periodo_ema:
            a = ancora_mobile(closes[max(0, i - periodo_ema * 3):i + 1], periodo_ema)
        else:
            a = closes[0]
        if not a:
            continue
        t = target_unita(p, a, step, max_unita)
        d = t - pos
        if abs(d) >= 1:
            contante -= d * p                 # compra = esce contante
            contante -= abs(d) * p * spread / 2
            pos = t
        somma_pos += pos
        if abs(pos) >= max_unita:
            al_tetto += 1
    n = max(1, len(closes) - minimo)
    valore = contante + pos * closes[-1]
    return {"pnl_pct": valore / closes[0] * 100, "al_tetto": al_tetto / n * 100,
            "pos_media": somma_pos / n}


def main() -> int:
    capital = CapitalClient(load_config())
    capital.login()
    epics = sys.argv[1:] or ["GOLD", "US100", "US500", "DE40", "BTCUSD", "ETHUSD"]
    print(f"{'epic':<9} {'deriva':>8} | {'FISSO':>26} | {'MOBILE (EMA 50)':>26}")
    print(f"{'':<9} {'':>8} | {'P&L':>8} {'%tetto':>8} {'posmed':>8} | "
          f"{'P&L':>8} {'%tetto':>8} {'posmed':>8}")
    dati = []
    for ep in epics:
        try:
            mk = capital.get_market(ep)
            sn = mk.get("snapshot", {})
            bid, off = float(sn["bid"]), float(sn["offer"])
            spread = (off - bid) / ((bid + off) / 2)
            closes = []
            for x in capital.get_prices(ep, resolution="DAY", max_bars=400):
                cp = x.get("closePrice") or {}
                if cp.get("bid") and cp.get("ask"):
                    closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
            if len(closes) < 150:
                continue
            deriva = (closes[-1] / closes[0] - 1) * 100
            f = simula(closes, 0.002, 10, spread, None)
            m = simula(closes, 0.002, 10, spread, 50)
            dati.append((deriva, f, m))
            print(f"{ep:<9} {deriva:>+7.1f}% | {f['pnl_pct']:>+7.1f}% {f['al_tetto']:>7.1f}% "
                  f"{f['pos_media']:>+7.2f} | {m['pnl_pct']:>+7.1f}% {m['al_tetto']:>7.1f}% "
                  f"{m['pos_media']:>+7.2f}")
        except Exception as exc:
            print(f"{ep:<9} errore: {str(exc)[:40]}")

    if len(dati) >= 3:
        def corr(xs, ys):
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
            den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
            return num / den if den else 0
        d = [x[0] for x in dati]
        print(f"\ncorrelazione deriva -> posizione media:")
        print(f"  ancoraggio FISSO : r = {corr(d, [x[1]['pos_media'] for x in dati]):+.3f}"
              f"   (vicino a -1 = incollato contro la tendenza)")
        print(f"  ancoraggio MOBILE: r = {corr(d, [x[2]['pos_media'] for x in dati]):+.3f}"
              f"   (vicino a 0 = neutrale)")
        print(f"\ntempo medio al tetto: fisso {sum(x[1]['al_tetto'] for x in dati)/len(dati):.1f}%"
              f"  ->  mobile {sum(x[2]['al_tetto'] for x in dati)/len(dati):.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
