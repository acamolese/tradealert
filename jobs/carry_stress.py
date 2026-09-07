"""Il carry trade sopravvive a un crollo? Storia lunga e conto dei danni.

Su 400 giorni le coppie a carry positivo mostrano rendimenti a doppia cifra con
perdite massime sotto il 5%. E' il profilo classico che inganna: questa
strategia e' nota per salire piano e scendere in verticale, e in 400 giorni di
calma non si vede. Qui si va indietro il piu' possibile (barre settimanali:
circa otto anni) e si misura la cosa che conta davvero:

  quanti anni di carry servono per recuperare il peggior crollo?

Se ne servono venti, la strategia raccoglie monetine davanti a un rullo
compressore. Se ne servono due, e' un rischio che si puo' gestire.

Uso: python -m jobs.carry_stress
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from src.config import load_config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
# carry annuo dichiarato dal broker oggi (i tassi cambiano nel tempo: e' una
# approssimazione, dichiarata, del premio incassato)
COPPIE = {"MXNJPY": 4.21, "AUDJPY": 1.83, "AUDCHF": 2.91, "AUDCAD": 0.55,
          "AUDSGD": 1.35, "NZDJPY": 0.55, "NZDCHF": 1.64, "CADCHF": 0.87}


def barre(cap, epic: str, res: str, n: int) -> list[dict]:
    f = CACHE / f"lungo_{res}_{epic}.json"
    if f.exists():
        return json.loads(f.read_text())
    try:
        raw = cap.get_prices(epic, resolution=res, max_bars=n)
    except Exception as exc:
        print(f"  {epic}: {type(exc).__name__}")
        return []
    out = []
    for b in raw:
        cp = b.get("closePrice") or {}
        try:
            out.append({"t": (b.get("snapshotTimeUTC") or "")[:10],
                        "p": (float(cp["bid"]) + float(cp["ask"])) / 2})
        except (KeyError, TypeError, ValueError):
            continue
    f.write_text(json.dumps(out))
    time.sleep(0.3)
    return out


def cali(px: list[dict]) -> list[tuple[float, str, str]]:
    """Tutte le discese dal massimo precedente, dalla peggiore in giu'."""
    fuori, picco, inizio = [], px[0]["p"], px[0]["t"]
    fondo, t_fondo = None, None
    for b in px:
        if b["p"] >= picco:
            if fondo is not None:
                fuori.append(((fondo / picco - 1) * 100, inizio, t_fondo))
                fondo = None
            picco, inizio = b["p"], b["t"]
        elif fondo is None or b["p"] < fondo:
            fondo, t_fondo = b["p"], b["t"]
    if fondo is not None:
        fuori.append(((fondo / picco - 1) * 100, inizio, t_fondo))
    return sorted(fuori)


def main() -> int:
    from src.capital_client import CapitalClient
    cap = CapitalClient(load_config())
    cap.login()
    CACHE.mkdir(parents=True, exist_ok=True)

    print(f"{'coppia':10s} {'storia':>18s} {'carry/anno':>11s} {'peggior crollo':>15s} "
          f"{'quando':>22s} {'anni per recuperare':>20s}")
    print("-" * 100)
    for ep, carry in COPPIE.items():
        px = barre(cap, ep, "WEEK", 400)
        if len(px) < 100:
            continue
        anni = len(px) / 52
        c = cali(px)
        peggio, da, a = c[0]
        recupero = abs(peggio) / carry if carry > 0 else 999
        print(f"{ep:10s} {px[0]['t']} ({anni:.1f}a) {carry:+10.2f}% {peggio:14.1f}% "
              f"{da + ' -> ' + a:>22s} {recupero:19.1f}")

    print("\nI cinque crolli peggiori di AUD/JPY, la coppia piu' rappresentativa:")
    px = barre(cap, "AUDJPY", "WEEK", 400)
    for perdita, da, a in cali(px)[:5]:
        print(f"  {perdita:6.1f}%   dal {da} al {a}   "
              f"({abs(perdita)/1.83:.1f} anni di carry bruciati)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
