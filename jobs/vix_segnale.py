"""Il VIX predice i rendimenti futuri degli indici? Test su otto anni.

Idea non nostra: la letteratura documenta che il livello del VIX e la pendenza
della sua struttura a termine anticipano il rendimento dell'S&P 500 (quando la
paura e' alta il premio al rischio richiesto e' alto, quindi i rendimenti
successivi lo sono). E' l'unica cosa che abbiamo provato che NON usa solo il
prezzo dello strumento su cui si opera: usa un'informazione esterna.

Capital quota il VIX (spot) e VIXM (futures a medio termine): il loro rapporto
approssima la pendenza della curva.

Uso: python -m jobs.vix_segnale
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from src.config import load_config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
INDICI = ["US500", "US100", "US30", "DE40"]


def storia(cap, epic: str, res: str = "WEEK", n: int = 400) -> dict[str, float]:
    f = CACHE / f"vixtest_{res}_{epic}.json"
    if f.exists():
        return json.loads(f.read_text())
    try:
        raw = cap.get_prices(epic, resolution=res, max_bars=n)
    except Exception as exc:
        print(f"  {epic}: {type(exc).__name__}")
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


def quantili(v: list[float], n: int = 5) -> list[float]:
    s = sorted(v)
    return [s[int(len(s) * i / n)] for i in range(1, n)]


def main() -> int:
    from src.capital_client import CapitalClient
    cap = CapitalClient(load_config())
    cap.login()
    CACHE.mkdir(parents=True, exist_ok=True)

    vix = storia(cap, "VIX")
    vixm = storia(cap, "VIXM")
    if len(vix) < 100:
        print("storia del VIX insufficiente")
        return 1
    print(f"VIX: {len(vix)} settimane dal {min(vix)}   VIXM: {len(vixm)} settimane\n")

    for epic in INDICI:
        px = storia(cap, epic)
        date = sorted(d for d in px if d in vix)
        if len(date) < 100:
            print(f"{epic}: dati insufficienti")
            continue
        liv = [vix[d] for d in date]
        soglie = quantili(liv)
        print(f"=== {epic} — {len(date)} settimane ===")
        print(f"{'quando il VIX e...':28s} {'settimane':>10s} {'rendimento 4 settimane dopo':>30s}")
        gruppi = [("molto basso (sotto %.1f)" % soglie[0], lambda v: v < soglie[0]),
                  ("basso", lambda v: soglie[0] <= v < soglie[1]),
                  ("normale", lambda v: soglie[1] <= v < soglie[2]),
                  ("alto", lambda v: soglie[2] <= v < soglie[3]),
                  ("molto alto (oltre %.1f)" % soglie[3], lambda v: v >= soglie[3])]
        for nome, test in gruppi:
            fut = []
            for i in range(len(date) - 4):
                if not test(vix[date[i]]):
                    continue
                a, b = px[date[i]], px[date[i + 4]]
                if a > 0:
                    fut.append((b / a - 1) * 100)
            if len(fut) < 10:
                continue
            m = statistics.mean(fut)
            se = statistics.stdev(fut) / (len(fut) ** 0.5) if len(fut) > 1 else 0
            t = m / se if se else 0
            print(f"  {nome:26s} {len(fut):10d} {m:+15.2f}%  (t {t:+.1f})")
        print()

    if len(vixm) > 100:
        print("=== pendenza della curva (VIXM / VIX) su US500 ===")
        px = storia(cap, "US500")
        date = sorted(d for d in px if d in vix and d in vixm and vix[d] > 0)
        rap = [vixm[d] / vix[d] for d in date]
        s = quantili(rap, 3)
        print(f"{'curva':28s} {'settimane':>10s} {'rendimento 4 settimane dopo':>30s}")
        for nome, test in (("ripida (calma)", lambda r: r >= s[1]),
                           ("normale", lambda r: s[0] <= r < s[1]),
                           ("piatta o invertita (stress)", lambda r: r < s[0])):
            fut = []
            for i in range(len(date) - 4):
                if not test(vixm[date[i]] / vix[date[i]]):
                    continue
                a, b = px[date[i]], px[date[i + 4]]
                if a > 0:
                    fut.append((b / a - 1) * 100)
            if len(fut) < 10:
                continue
            m = statistics.mean(fut)
            se = statistics.stdev(fut) / (len(fut) ** 0.5)
            print(f"  {nome:26s} {len(fut):10d} {m:+15.2f}%  (t {m/se if se else 0:+.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
