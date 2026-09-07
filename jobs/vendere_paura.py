"""Vendere assicurazione invece di prevedere i prezzi.

Diciassette ipotesi hanno cercato un segnale che dicesse dove andra' il mercato,
e nessuna ha retto. Questa non prevede niente: sfrutta il fatto che gli
strumenti che comprano protezione dalla volatilita' perdono valore per come sono
costruiti (rinnovano contratti a termine piu' cari di quelli che scadono).
Misurato su quindici anni: UVXY -79,8% l'anno, VIXY -51%, VXX -41,3%, con una
regolarita' che nessun altro fenomeno di questo progetto ha mostrato.

Chi sta dall'altra parte incassa quel decadimento. E' il premio dell'assicuratore:
si guadagna quasi sempre poco e si perde raramente molto. Il 5 febbraio 2018
UVXY e' salito del 66% in una seduta.

Qui si simula con i costi VERI di Capital (spread 0,74%, finanziamento 0,23%
l'anno per la posizione corta) e ribilanciamento a frequenze diverse.

Uso: python -m jobs.vendere_paura
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "yahoo"
SPREAD = {"UVXY": 0.741, "VIXM": 0.681, "UVIX": 0.343}
FIN_SHORT_GG = 0.23 / 365


def serie(tk: str) -> list[tuple[str, float]]:
    f = CACHE / f"{tk}_1d.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text())
    return [(k, d[k]) for k in sorted(d)]


def simula(px, esposizione: float, ogni: int, spread: float,
           stop: float | None = None):
    """Short ribilanciato ogni ``ogni`` giorni. Ritorna rendimento annuo,
    peggior calo, giorno peggiore, quante volte lo stop e' scattato."""
    cap, picco, peggio, worst, stop_colpiti = 1.0, 1.0, 0.0, 0.0, 0
    quota = esposizione
    for i in range(1, len(px)):
        r = px[i][1] / px[i - 1][1] - 1
        perdita = -quota * r
        if stop and perdita < -stop:
            perdita = -stop
            stop_colpiti += 1
            quota = 0.0
        worst = min(worst, perdita * 100)
        cap *= (1 + perdita)
        cap *= (1 - quota * FIN_SHORT_GG / 100)
        if cap <= 0:
            return -100.0, -100.0, worst, stop_colpiti
        picco = max(picco, cap)
        peggio = min(peggio, (cap / picco - 1) * 100)
        if i % ogni == 0 or quota == 0.0:
            costo = abs(esposizione - quota) if quota == 0 else esposizione * 0.3
            cap *= (1 - costo * spread / 100)
            quota = esposizione
    anni = len(px) / 252
    return (cap ** (1 / anni) - 1) * 100, peggio, worst, stop_colpiti


def main() -> int:
    print("Vendere allo scoperto chi compra volatilita', con i costi veri di Capital\n")
    print(f"{'strum.':7s} {'esposto':>8s} {'ribilancia':>11s} {'stop':>6s} "
          f"{'rendim./anno':>13s} {'peggior calo':>13s} {'giorno peggiore':>16s} {'stop scattati':>14s}")
    print("-" * 96)
    for tk in ("UVXY", "VIXM"):
        px = serie(tk)
        if len(px) < 500:
            continue
        for esp in (0.15, 0.25, 0.40):
            for ogni, et in ((21, "mensile"), (63, "trimestrale")):
                for stop in (None, 0.08):
                    a, p, w, sc = simula(px, esp, ogni, SPREAD[tk], stop)
                    print(f"{tk:7s} {esp:7.0%} {et:>11s} "
                          f"{('-' if stop is None else format(stop, '.0%')):>6s} "
                          f"{a:+12.1f}% {p:12.1f}% {w:15.1f}% {sc:14d}")
        print()
    print("riferimento: l'S&P 500 negli stessi quindici anni ha reso +15,4% l'anno")
    print("con un calo massimo del 34%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
