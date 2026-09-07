"""Il movimento continua o torna indietro? Misura su tutto il paniere possibile.

Domanda dell'utente (2026-09-07): "se un titolo inizia a perdere puo' essere che
perdera' ancora, entro e prendo quello che perde". E' l'ipotesi del MOMENTUM, ed
e' l'opposto di quella su cui e' costruito il grid (che sui cali compra).

Una prima misura su tre strumenti per gruppo diceva: niente segnale sugli indici
azionari, momentum su valute e materie prime. Troppo pochi dati per crederci.
Qui si allarga a tutti gli strumenti comprabili con un capitale piccolo, su due
risoluzioni indipendenti (400 giorni di barre giornaliere, 1000 barre orarie).

Il numero che conta e' l'EDGE al netto della deriva:

    edge = (rendimento dopo un rialzo - rendimento dopo un calo) / 2

positivo = il movimento continua (momentum, si entra nella sua direzione);
negativo = il movimento rientra (si entra contro). La deriva del mercato si
elide da sola nella differenza, quindi non serve stimarla.

Va poi confrontato con il costo: spread del giro piu' finanziamento per le
notti che la posizione resta aperta.

Uso:
  python -m jobs.momentum_paniere [--max-nozionale 100] [--min-storia 200]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from src.config import load_config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
CAMPO = CACHE / "campo_da_gioco.json"
NOSTRI = {"US100", "US30", "US500", "DE40", "NL25", "J225", "HK50", "GOLD"}


def _arg(nome, default):
    if nome in sys.argv:
        try:
            return type(default)(sys.argv[sys.argv.index(nome) + 1])
        except (IndexError, ValueError):
            pass
    return default


def chiusure(cap, epic: str, res: str, barre: int) -> list[float]:
    f = CACHE / f"mom_{res}_{epic}.json"
    if f.exists():
        return json.loads(f.read_text())
    try:
        px = cap.get_prices(epic, resolution=res, max_bars=barre)
    except Exception:
        return []
    out = []
    for b in px:
        cp = b.get("closePrice") or {}
        try:
            out.append((float(cp["bid"]) + float(cp["ask"])) / 2)
        except (KeyError, TypeError, ValueError):
            continue
    f.write_text(json.dumps(out))
    time.sleep(0.25)
    return out


def edge(px: list[float], L: int, F: int) -> tuple[float, int] | None:
    """Quanto rende entrare NELLA direzione del movimento appena avvenuto."""
    su, giu = [], []
    for i in range(L, len(px) - F):
        if px[i - L] <= 0 or px[i] <= 0:
            continue
        passato = px[i] / px[i - L] - 1
        futuro = (px[i + F] / px[i] - 1) * 100
        (su if passato > 0 else giu).append(futuro)
    if len(su) < 30 or len(giu) < 30:
        return None
    return (statistics.mean(su) - statistics.mean(giu)) / 2, len(su) + len(giu)


def main() -> int:
    max_noz = _arg("--max-nozionale", 100.0)
    try:
        universo = json.loads(CAMPO.read_text())
    except Exception:
        print("manca data/cache/campo_da_gioco.json: lancia prima jobs.campo_da_gioco")
        return 1

    cand = [m for m in universo if m["nozionale"] <= max_noz]
    print(f"{len(cand)} strumenti comprabili con questo capitale\n")

    from src.capital_client import CapitalClient
    cap = CapitalClient(load_config())
    cap.login()

    ORIZZONTI = [("DAY", 400, [(1, 1, "1 giorno"), (3, 3, "3 giorni"), (5, 5, "5 giorni")]),
                 ("HOUR", 1000, [(4, 4, "4 ore"), (12, 12, "12 ore"), (24, 24, "1 giorno")])]

    per_tipo = defaultdict(lambda: defaultdict(list))
    per_strumento = {}
    for i, m in enumerate(cand, 1):
        if i % 15 == 0:
            print(f"  ...{i}/{len(cand)}")
        tipo = "INDICI" if m["tipo"] == "INDICES" else (
            "VALUTE" if m["tipo"] == "CURRENCIES" else (
                "MATERIE PRIME" if m["tipo"].startswith("COMMODIT") else m["tipo"][:12]))
        riga = {"nome": m["nome"], "tipo": tipo, "spread": m["spread_pct"],
                "fin_long": m["fin_long"], "fin_short": m["fin_short"], "edge": {}}
        for res, nb, finestre in ORIZZONTI:
            px = chiusure(cap, m["epic"], res, nb)
            if len(px) < 150:
                continue
            for L, F, nome in finestre:
                r = edge(px, L, F)
                if r:
                    per_tipo[tipo][nome].append(r[0])
                    riga["edge"][nome] = r[0]
        if riga["edge"]:
            per_strumento[m["epic"]] = riga

    print(f"\n{len(per_strumento)} strumenti con storia sufficiente\n")
    print("EDGE MEDIO per gruppo (positivo = il movimento continua, negativo = rientra)")
    nomi = ["4 ore", "12 ore", "1 giorno", "3 giorni", "5 giorni"]
    print(f"{'gruppo':16s} {'n':>4s} " + " ".join(f"{n:>10s}" for n in nomi))
    print("-" * 74)
    for tipo in sorted(per_tipo):
        n = max(len(v) for v in per_tipo[tipo].values())
        celle = []
        for nome in nomi:
            v = per_tipo[tipo].get(nome, [])
            celle.append(f"{statistics.mean(v):+9.4f}%" if len(v) >= 3 else f"{'-':>10s}")
        print(f"{tipo:16s} {n:4d} " + " ".join(celle))

    print("\nI MIGLIORI per momentum a 1 giorno, con il costo da battere:")
    ordinati = sorted((r for r in per_strumento.values() if "1 giorno" in r["edge"]),
                      key=lambda r: -abs(r["edge"]["1 giorno"]))
    print(f"{'strumento':30s} {'tipo':14s} {'edge/gg':>9s} {'spread':>8s} {'notte L':>8s} {'notte S':>8s}")
    for r in ordinati[:22]:
        print(f"{r['nome'][:29]:30s} {r['tipo']:14s} {r['edge']['1 giorno']:+8.4f}% "
              f"{r['spread']:7.4f}% {abs(r['fin_long'])/365:7.4f}% {abs(r['fin_short'])/365:7.4f}%")

    (CACHE / "momentum_paniere.json").write_text(
        json.dumps(per_strumento, indent=1, ensure_ascii=False))
    print(f"\ndati completi in {CACHE / 'momentum_paniere.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
