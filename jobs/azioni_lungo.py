"""Momentum fra azioni su vent'anni, con dati veri e universo ampio.

Su Capital il test era impossibile: due anni di storia e quaranta titoli scelti
per lo spread di oggi, cioe' selezionati sui vincitori. Qui la storia arriva da
un archivio pubblico (vent'anni, barre settimanali) su un universo ampio.

Sul bias che resta, dichiarato: l'universo e' l'S&P 500 di oggi, quindi mancano
le aziende che sono fallite o uscite dall'indice. Questo pero' PENALIZZA il
momentum invece di gonfiarlo: i "perdenti" del nostro campione sono quelli che
poi si sono ripresi, e sono proprio quelli che la strategia vende. Se il segnale
sopravvive lo stesso, e' un indizio piu' forte, non piu' debole.

Uso: python -m jobs.azioni_lungo [--anni 20] [--scarica]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "yahoo"
COSTO_ANNO = 8.11 + 2.1     # finanziamento delle due gambe + spread di ribilanciamento

TITOLI = """AAPL MSFT NVDA AMZN GOOGL META TSLA BRK-B LLY AVGO JPM XOM UNH V PG MA
HD COST MRK ABBV CVX ADBE PEP KO WMT CRM BAC TMO MCD CSCO ACN ABT LIN NFLX AMD
DHR TXN NEE DIS WFC PM VZ INTC CMCSA INTU COP AMGN IBM QCOM NKE UNP CAT SPGI
LOW HON GE BA AXP BKNG SBUX GS DE ELV BLK MDT ADP GILD LMT SYK TJX MMC ISRG
VRTX REGN ZTS CI SCHW MO CB SO PLD DUK BSX EQIX AON ITW SLB APD MU ETN NOC
CSX WM FCX EMR MCK GM PSA MSI ROP APH ORLY CME AJG NSC TGT HCA MAR PCAR FDX
AIG MET AFL TRV ALL PRU DOW DD ECL SHW NEM VLO PSX MPC OXY HAL KMI WMB OKE
F HAS EBAY EXPE DAL UAL LUV RCL CCL MGM WYNN LVS KHC GIS K HSY SJM CAG CPB
CLX KMB CHD EL COTY YUM CMG DRI DPZ ROST BURL GPS ANF AEO LULU DECK CROX SKX
UPS ODFL JBHT CHRW EXPD LSTR SAIA XPO ARCB MATX KEX GATX TRN WAB CMI PCAR"""


def _arg(nome, default):
    if nome in sys.argv:
        try:
            return type(default)(sys.argv[sys.argv.index(nome) + 1])
        except (IndexError, ValueError):
            pass
    return default


def scarica(ticker: str, anni: int) -> dict[str, float]:
    f = CACHE / f"{ticker}.json"
    if f.exists():
        return json.loads(f.read_text())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={anni}y&interval=1wk")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
    except Exception:
        f.write_text("{}")
        return {}
    try:
        res = d["chart"]["result"][0]
        ts = res["timestamp"]
        cl = res["indicators"]["adjclose"][0]["adjclose"]
    except (KeyError, IndexError, TypeError):
        f.write_text("{}")
        return {}
    out = {}
    for t, c in zip(ts, cl):
        if c:
            # chiave per SETTIMANA, non per data: gli exchange hanno fusi diversi
            # e le stesse barre settimanali cadono su giorni diversi
            d = datetime.fromtimestamp(t, tz=timezone.utc).date()
            a, w, _ = d.isocalendar()
            out[f"{a}-{w:02d}"] = float(c)
    f.write_text(json.dumps(out))
    time.sleep(0.15)
    return out


def prova(dati, com, lb, ten, lato, da=None, a=None):
    date = [d for d in com if (not da or d >= da) and (not a or d < a)]
    res, i = [], lb
    while i + ten < len(date):
        t0, t1, t2 = date[i - lb], date[i], date[i + ten]
        perf = [(dati[e][t1] / dati[e][t0] - 1, e) for e in dati if dati[e][t0] > 0]
        if len(perf) < lato * 2 + 4:
            i += ten
            continue
        perf.sort(reverse=True)
        rs = statistics.mean((dati[e][t2] / dati[e][t1] - 1) * 100 for _, e in perf[:lato])
        rg = statistics.mean((dati[e][t2] / dati[e][t1] - 1) * 100 for _, e in perf[-lato:])
        res.append(rs - rg)
        i += ten
    if len(res) < 8:
        return None
    m = statistics.mean(res)
    se = statistics.stdev(res) / (len(res) ** 0.5)
    return m * 52 / ten - COSTO_ANNO, (m / se if se else 0), len(res)


def main() -> int:
    anni = _arg("--anni", 20)
    CACHE.mkdir(parents=True, exist_ok=True)
    tk = TITOLI.split()
    print(f"scarico {len(tk)} titoli, {anni} anni di barre settimanali...")
    dati = {}
    for i, t in enumerate(tk, 1):
        s = scarica(t, anni)
        if len(s) > 200:
            dati[t] = s
        if i % 40 == 0:
            print(f"  ...{i}/{len(tk)}")
    if len(dati) < 30:
        print("dati insufficienti")
        return 1
    com = sorted(set.intersection(*(set(v) for v in dati.values())))
    print(f"\n{len(dati)} titoli con storia, {len(com)} settimane in comune "
          f"({len(com)/52:.1f} anni) dal {com[0]}\n")

    print(f"{'finestra':>9s} {'tiene':>6s} {'lato':>5s} {'lordo/anno':>11s} "
          f"{'NETTO/anno':>11s} {'t':>6s} {'coppie':>7s}")
    print("-" * 62)
    for lb in (4, 12, 26, 52):
        for ten in (4, 12):
            for lato in (10, 20):
                r = prova(dati, com, lb, ten, lato)
                if r:
                    marchio = "  <--" if r[0] > 0 and abs(r[1]) > 3 else ""
                    print(f"{lb:7d}s {ten:5d}s {lato:5d} {r[0]+COSTO_ANNO:+10.1f}% "
                          f"{r[0]:+10.1f}% {r[1]:+6.1f} {r[2]:7d}{marchio}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
