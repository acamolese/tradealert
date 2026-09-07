"""Momentum fra AZIONI: dove la dispersione esiste davvero.

Sugli otto indici il confronto fra strumenti non aveva senso: si muovono insieme
all'85%, ordinarli e' ordinare rumore. Sulle azioni singole la dispersione fra il
migliore e il peggiore e' di un altro ordine di grandezza, ed e' li' che la
letteratura documenta il momentum trasversale.

Perimetro: le azioni ed ETF con spread sotto lo 0,1% (le uniche su cui il costo
non divora tutto). Posizione bilanciata: si compra il gruppo migliore e si vende
il peggiore, quindi il movimento generale del mercato si elide.

Il costo da battere resta pesante: 8,11% l'anno di finanziamento sulle due gambe.

Uso: python -m jobs.azioni_incrociato [--titoli 40] [--spread-max 0.12]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from src.config import load_config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
COSTO_ANNO = 8.11


def _arg(nome, default):
    if nome in sys.argv:
        try:
            return type(default)(sys.argv[sys.argv.index(nome) + 1])
        except (IndexError, ValueError):
            pass
    return default


def storia(cap, epic: str) -> dict[str, float]:
    f = CACHE / f"az_WEEK_{epic}.json"
    if f.exists():
        return json.loads(f.read_text())
    try:
        raw = cap.get_prices(epic, resolution="WEEK", max_bars=200)
    except Exception:
        f.write_text("{}")
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
    time.sleep(0.2)
    return out


def main() -> int:
    n_titoli = _arg("--titoli", 40)
    spread_max = _arg("--spread-max", 0.12)
    uni = json.loads((CACHE / "universo_azioni.json").read_text())
    cand = sorted((m for m in uni if 0 < m["spread"] <= spread_max),
                  key=lambda x: x["spread"])[:n_titoli]
    print(f"{len(cand)} titoli con spread sotto lo {spread_max}%\n")

    from src.capital_client import CapitalClient
    cap = CapitalClient(load_config())
    cap.login()
    dati = {}
    for i, m in enumerate(cand, 1):
        s = storia(cap, m["epic"])
        if len(s) > 100:
            dati[m["epic"]] = s
        if i % 10 == 0:
            print(f"  ...{i}/{len(cand)}")
    comuni = sorted(set.intersection(*(set(v) for v in dati.values()))) if dati else []
    print(f"\n{len(dati)} titoli con storia, {len(comuni)} settimane in comune")
    if len(comuni) < 60:
        print("storia insufficiente")
        return 1

    # quanta differenza c'e' fra il migliore e il peggiore, settimana per settimana
    disp = []
    for i in range(1, len(comuni)):
        r = [(dati[e][comuni[i]] / dati[e][comuni[i - 1]] - 1) * 100
             for e in dati if dati[e][comuni[i - 1]] > 0]
        if len(r) > 5:
            disp.append(max(r) - min(r))
    print(f"dispersione settimanale fra migliore e peggiore: {statistics.median(disp):.1f}% "
          f"(sugli indici era circa 3%)\n")

    print(f"{'guarda indietro':>16s} {'tiene':>8s} {'quanti per lato':>16s} "
          f"{'lordo/anno':>11s} {'NETTO/anno':>11s} {'t':>6s} {'coppie':>7s}")
    print("-" * 84)
    migliori = []
    for lookback in (4, 12, 26, 52):
        for tenuta in (4, 12):
            for lato in (3, 5, 8):
                res = []
                i = lookback
                while i + tenuta < len(comuni):
                    t0, t1, t2 = comuni[i - lookback], comuni[i], comuni[i + tenuta]
                    perf = [(dati[e][t1] / dati[e][t0] - 1, e) for e in dati
                            if dati[e][t0] > 0 and dati[e][t1] > 0]
                    if len(perf) < lato * 2 + 2:
                        i += tenuta
                        continue
                    perf.sort(reverse=True)
                    su = [e for _, e in perf[:lato]]
                    giu = [e for _, e in perf[-lato:]]
                    rs = statistics.mean((dati[e][t2] / dati[e][t1] - 1) * 100 for e in su)
                    rg = statistics.mean((dati[e][t2] / dati[e][t1] - 1) * 100 for e in giu)
                    res.append(rs - rg)
                    i += tenuta
                if len(res) < 8:
                    continue
                per_anno = 52 / tenuta
                lordo = statistics.mean(res) * per_anno
                se = statistics.stdev(res) / (len(res) ** 0.5) if len(res) > 1 else 0
                t = statistics.mean(res) / se if se else 0
                netto = lordo - COSTO_ANNO
                m = "  <-- batte il costo" if netto > 0 and abs(t) > 2 else ""
                migliori.append((netto, t, lookback, tenuta, lato))
                print(f"{lookback:13d} sett {tenuta:6d} {lato:16d} {lordo:+10.1f}% "
                      f"{netto:+10.1f}% {t:+6.1f} {len(res):7d}{m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
