"""Dove un sistema che compra e vende ha spazio: movimento contro costo.

Domanda del 2026-09-07, dopo che il grid sugli otto indici ha reso rumore: un
sistema che entra ed esce vive del rapporto tra quanto il prezzo si muove e
quanto costa muoversi. Sugli indici attuali quel rapporto e' circa 8 a 1
(oscillazione tipica catturabile 0,3%, spread 0,04%): troppo sottile perche' il
segno del risultato dipenda dalle decisioni invece che dal caso.

Qui si misura lo stesso rapporto su tutto l'universo negoziabile, senza
prevedere nulla: sono tutti dati che il broker gia' espone.

  movimento  = mediana dell'ampiezza giornaliera (max-min)/chiusura
  costo      = spread corrente in percento (un giro completo ne paga uno)
  rapporto   = movimento / costo, quante volte al giorno il prezzo copre il
               pedaggio di un'operazione

Il filtro che conta con un capitale piccolo e' il NOZIONALE della taglia
minima: se la piu' piccola operazione possibile vale 500 EUR, con 200 EUR di
capitale quello strumento non esiste.

Uso:
  python -m jobs.campo_da_gioco [--capitale 200] [--max-nozionale 100]
                                [--candidati 120] [--giorni 60] [--csv]
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
from statistics import median

from src.config import load_config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
NOSTRI = {"US100", "US30", "US500", "DE40", "NL25", "J225", "HK50", "GOLD"}
# le azioni singole sono migliaia e portano rischio d'impresa: fuori da questa passata
CATEGORIE_ESCLUSE = {"hierarchy_v1.shares"}


def _arg(nome, default):
    if nome in sys.argv:
        try:
            return type(default)(sys.argv[sys.argv.index(nome) + 1])
        except (IndexError, ValueError):
            pass
    return default


def raccogli_universo(cap) -> list[dict]:
    """Scende nella gerarchia dei mercati e raccoglie le foglie negoziabili."""
    visti, out, coda = set(), {}, []
    for n in (cap.get_market_navigation().get("nodes") or []):
        if n.get("id") not in CATEGORIE_ESCLUSE:
            coda.append(n["id"])
    while coda:
        nodo = coda.pop(0)
        if nodo in visti:
            continue
        visti.add(nodo)
        try:
            d = cap.get_market_navigation(nodo)
        except Exception:
            continue
        for n in (d.get("nodes") or []):
            if n.get("id") not in visti and n.get("id") not in CATEGORIE_ESCLUSE:
                coda.append(n["id"])
        for m in (d.get("markets") or []):
            ep = m.get("epic")
            if not ep or ep in out:
                continue
            bid, ask = m.get("bid"), m.get("offer")
            if not bid or not ask or float(bid) <= 0:
                continue
            spread = (float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2) * 100
            out[ep] = {"epic": ep, "nome": m.get("instrumentName") or ep,
                       "tipo": m.get("instrumentType") or "?",
                       "stato": m.get("marketStatus") or "?",
                       "mid": (float(ask) + float(bid)) / 2, "spread_pct": spread}
        time.sleep(0.12)
    return list(out.values())


def misura(cap, m: dict, giorni: int) -> dict | None:
    """Nozionale della taglia minima, costo di finanziamento, movimento tipico."""
    from src.executor import _market_meta
    from src.risk import quote_to_ref_factor
    try:
        mk = cap.get_market(m["epic"])
    except Exception:
        return None
    inst = mk.get("instrument") or {}
    try:
        meta = _market_meta(mk)
    except Exception:
        return None
    q2r = quote_to_ref_factor(inst.get("currency"), cap) or 1.0
    m["nozionale"] = meta["min_size"] * m["mid"] * q2r
    m["min_size"] = meta["min_size"]
    o = inst.get("overnightFee") or {}
    m["fin_long"] = float(o.get("longRate") or 0) * 365
    m["fin_short"] = float(o.get("shortRate") or 0) * 365
    return m


def movimento(cap, m: dict, giorni: int) -> dict | None:
    """Ampiezza giornaliera mediana, in percento della chiusura."""
    try:
        px = cap.get_prices(m["epic"], resolution="DAY", max_bars=giorni)
    except Exception:
        return None
    amp = []
    for b in px:
        hi, lo, cl = b.get("highPrice") or {}, b.get("lowPrice") or {}, b.get("closePrice") or {}
        try:
            h = (float(hi["bid"]) + float(hi["ask"])) / 2
            l = (float(lo["bid"]) + float(lo["ask"])) / 2
            c = (float(cl["bid"]) + float(cl["ask"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
        if c > 0 and h >= l:
            amp.append((h - l) / c * 100)
    if len(amp) < 20:
        return None
    m["giorni"] = len(amp)
    m["movimento"] = median(amp)
    m["rapporto"] = m["movimento"] / m["spread_pct"] if m["spread_pct"] > 0 else 0
    return m


def main() -> int:
    capitale = _arg("--capitale", 200.0)
    max_noz = _arg("--max-nozionale", 100.0)
    candidati = _arg("--candidati", 120)
    giorni = _arg("--giorni", 60)

    from src.capital_client import CapitalClient
    cap = CapitalClient(load_config())
    cap.login()

    print("raccolgo l'universo negoziabile...")
    uni = raccogli_universo(cap)
    print(f"  {len(uni)} strumenti con prezzo (azioni singole escluse)")

    uni.sort(key=lambda x: x["spread_pct"])
    corti = uni[:candidati]
    print(f"  esamino i {len(corti)} con lo spread piu' basso "
          f"(da {corti[0]['spread_pct']:.4f}% a {corti[-1]['spread_pct']:.4f}%)")

    fuori_taglia = 0
    righe = []
    for i, m in enumerate(corti, 1):
        if i % 20 == 0:
            print(f"    ...{i}/{len(corti)}")
        if not misura(cap, m, giorni):
            continue
        if m["nozionale"] > max_noz:
            fuori_taglia += 1
            continue
        if movimento(cap, m, giorni):
            righe.append(m)
        time.sleep(0.15)

    print(f"  {fuori_taglia} scartati: la taglia minima supera {max_noz:.0f}€ di nozionale")
    righe.sort(key=lambda x: -x["rapporto"])

    print(f"\n{'strumento':34s} {'tipo':10s} {'movim.':>7s} {'spread':>8s} {'rapp.':>6s} "
          f"{'noz.min':>8s} {'fin.long':>8s} {'fin.short':>9s}")
    print("-" * 100)
    for m in righe[:40]:
        marchio = " *" if m["epic"] in NOSTRI else "  "
        print(f"{marchio}{m['nome'][:31]:32s} {m['tipo'][:10]:10s} {m['movimento']:6.2f}% "
              f"{m['spread_pct']:7.3f}% {m['rapporto']:6.0f} {m['nozionale']:7.1f}€ "
              f"{m['fin_long']:7.1f}% {m['fin_short']:8.1f}%")

    nostri = [m for m in righe if m["epic"] in NOSTRI]
    if nostri:
        print(f"\n(* = strumenti di ClaudeTrade. Il loro rapporto: "
              + ", ".join(f"{m['epic']} {m['rapporto']:.0f}" for m in nostri) + ")")

    out = CACHE / "campo_da_gioco.json"
    CACHE.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(righe, indent=1, ensure_ascii=False))
    print(f"\ndati completi in {out}")
    if "--csv" in sys.argv:
        f = CACHE / "campo_da_gioco.csv"
        with open(f, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(righe[0].keys()))
            w.writeheader()
            w.writerows(righe)
        print(f"csv in {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
