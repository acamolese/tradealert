"""Aumentare l'esposizione quando il mercato e' agitato: regge a un crollo?

Su 180 giorni la cosa funziona benissimo (rendimento raddoppiato). Ma quel
periodo non contiene un crollo, e questa e' esattamente la strategia che in un
crollo prolungato amplifica le perdite invece di ridurle. Qui si simula su
sette anni di barre settimanali, che contengono il Covid (marzo 2020) e il
mercato orso del 2022.

Regola: esposizione proporzionale alla volatilita' delle ultime settimane,
ribilanciata solo quando cambia di piu' di un quarto. Confronto con esposizione
fissa. Nessuna previsione di direzione.

Uso: python -m jobs.esposto_paura
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
INDICI = ["US500", "US100", "US30", "DE40"]
COSTO_SETT = 0.0216 * 7      # finanziamento settimanale, in percento


def serie(ep: str) -> list[tuple[str, float]]:
    f = CACHE / f"vixtest_WEEK_{ep}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text())
    return sorted(d.items())


def simula(px: list[tuple[str, float]], modo: str, finestra: int = 8,
           rif: float = 0.015) -> tuple[list[float], float]:
    """Ritorna la curva del capitale e il peggior calo. modo: fisso|paura|inverso."""
    cap, curva, peso, picco, peggio = 1.0, [], 1.0, 1.0, 0.0
    for i in range(finestra, len(px) - 1):
        fin = [p for _, p in px[i - finestra:i]]
        r = [abs(fin[j] / fin[j - 1] - 1) for j in range(1, len(fin))]
        sig = statistics.mean(r) if r else 0
        if sig > 0:
            if modo == "paura":
                nuovo = max(0.2, min(3.0, sig / rif))
            elif modo == "inverso":
                nuovo = max(0.2, min(3.0, rif / sig))
            else:
                nuovo = 1.0
            if abs(nuovo / peso - 1) > 0.25:
                peso = nuovo
        rend = (px[i + 1][1] / px[i][1] - 1) * 100 - COSTO_SETT
        cap *= (1 + peso * rend / 100)
        curva.append(cap)
        picco = max(picco, cap)
        peggio = min(peggio, (cap / picco - 1) * 100)
    return curva, peggio


print("Sette anni di barre settimanali, Covid e 2022 compresi.")
print("Rendimento annuo composto, al netto del finanziamento.\n")
print(f"{'indice':8s} {'esposizione fissa':>28s} {'esposto alla paura':>28s} "
      f"{'dosato al contrario':>28s}")
print(f"{'':8s} {'annuo':>12s} {'peggior calo':>15s} {'annuo':>12s} {'peggior calo':>15s} "
      f"{'annuo':>12s} {'peggior calo':>15s}")
print("-" * 96)
for ep in INDICI:
    px = serie(ep)
    if len(px) < 200:
        continue
    riga = f"{ep:8s}"
    for modo in ("fisso", "paura", "inverso"):
        curva, peggio = simula(px, modo)
        anni = len(curva) / 52
        annuo = (curva[-1] ** (1 / anni) - 1) * 100
        riga += f" {annuo:+11.1f}% {peggio:14.1f}%"
    print(riga)

print("\nGli anni singoli su US500 (rendimento dell'anno):")
px = serie("US500")
print(f"{'anno':6s} {'fissa':>10s} {'paura':>10s}")
for anno in ("2019","2020","2021","2022","2023","2024","2025","2026"):
    sub = [(d, p) for d, p in px if d[:4] == anno]
    if len(sub) < 30:
        continue
    r = []
    for modo in ("fisso", "paura"):
        curva, _ = simula(sub, modo)
        r.append((curva[-1] - 1) * 100 if curva else 0)
    print(f"{anno:6s} {r[0]:+9.1f}% {r[1]:+9.1f}%")
