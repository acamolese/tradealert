"""Momentum fra strumenti: comprare chi va meglio, vendere chi va peggio.

Diverso da tutto quello provato finora: non chiede se un mercato salira', chiede
quale dei nostri otto salira' PIU' DEGLI ALTRI. La posizione e' bilanciata (si
compra e si vende in parti uguali), quindi il movimento generale del mercato si
elide e resta solo la differenza. E' l'unica forma in cui il risultato sarebbe
davvero abilita' e non esposizione.

La letteratura lo da' come indebolito ma non morto, e suggerisce di scalarlo
sulla volatilita' (che e' quello che abbiamo appena implementato).

Il vincolo da battere e' pesante e va detto subito: su CFD una gamba comprata
costa il 7,88% l'anno di finanziamento e una venduta lo 0,23%, quindi una
coppia bilanciata parte con l'8,11% l'anno di svantaggio.

Uso: python -m jobs.momentum_incrociato
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from src.config import load_config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
EPICS = ["US500", "US100", "US30", "DE40", "NL25", "J225", "HK50", "GOLD"]
COSTO_ANNO = 8.11          # finanziamento di una coppia comprato+venduto


def storia(cap, epic: str) -> dict[str, float]:
    f = CACHE / f"vixtest_WEEK_{epic}.json"
    if f.exists():
        return json.loads(f.read_text())
    try:
        raw = cap.get_prices(epic, resolution="WEEK", max_bars=400)
    except Exception:
        return {}
    out = {}
    for b in raw:
        cp = b.get("closePrice") or {}
        t = (b.get("snapshotTimeUTC") or "")[:10]
        try:
            out[t] = (float(cp["bid"]) + float(cp["ask"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
    f.write_text(json.dumps(out))
    time.sleep(0.3)
    return out


def main() -> int:
    from src.capital_client import CapitalClient
    cap = CapitalClient(load_config())
    cap.login()
    dati = {ep: storia(cap, ep) for ep in EPICS}
    dati = {k: v for k, v in dati.items() if len(v) > 200}
    comuni = sorted(set.intersection(*(set(v) for v in dati.values())))
    print(f"{len(dati)} strumenti, {len(comuni)} settimane in comune "
          f"dal {comuni[0]} al {comuni[-1]}\n")

    print(f"{'guarda indietro':>16s} {'tiene per':>10s} {'quante coppie':>14s} "
          f"{'lordo/anno':>11s} {'costo':>8s} {'NETTO/anno':>11s} {'t':>6s}")
    print("-" * 82)
    for lookback in (4, 8, 12, 26):
        for tenuta in (1, 4, 8):
            for n_lati in (1, 2):
                risultati = []
                i = lookback
                while i + tenuta < len(comuni):
                    t0, t1, t2 = comuni[i - lookback], comuni[i], comuni[i + tenuta]
                    perf = []
                    for ep, s in dati.items():
                        if s[t0] > 0:
                            perf.append((s[t1] / s[t0] - 1, ep))
                    if len(perf) < 4:
                        i += tenuta
                        continue
                    perf.sort(reverse=True)
                    su = [ep for _, ep in perf[:n_lati]]
                    giu = [ep for _, ep in perf[-n_lati:]]
                    r_su = statistics.mean((dati[e][t2] / dati[e][t1] - 1) * 100 for e in su)
                    r_giu = statistics.mean((dati[e][t2] / dati[e][t1] - 1) * 100 for e in giu)
                    risultati.append(r_su - r_giu)
                    i += tenuta
                if len(risultati) < 20:
                    continue
                per_anno = 52 / tenuta
                lordo = statistics.mean(risultati) * per_anno
                se = statistics.stdev(risultati) / (len(risultati) ** 0.5)
                t = statistics.mean(risultati) / se if se else 0
                netto = lordo - COSTO_ANNO
                marchio = "  <-- batte il costo" if netto > 0 and t > 2 else ""
                print(f"{lookback:13d} sett {tenuta:8d} sett {n_lati:14d} "
                      f"{lordo:+10.1f}% {COSTO_ANNO:7.1f}% {netto:+10.1f}% {t:+6.1f}{marchio}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
