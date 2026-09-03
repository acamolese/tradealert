"""Backtest del grid2 alla FREQUENZA DEL CRON (barre 15 min), non a barre DAY.

Perche' esiste (2026-09-03): `grid_anchor_test.py` validava la configurazione su
barre giornaliere ("82% di finestre positive"), ma il job gira ogni 10 minuti e
a quella risoluzione la logica `target = -floor(...)` compra appena sotto p0 e
vende appena sopra: 250 coppie di fill reali, il 79% a meno dello 0.15% di
distanza. La simulazione giornaliera non poteva vederlo. Questa replica la
funzione pura del job barra per barra, con lo spread reale di ogni barra e un
costo overnight stimato dai SWAP addebitati, e confronta:

  - logica attuale (floor) vs isteresi (`target_isteresi`);
  - ancoraggio EMA5 con la barra del giorno in corso (com'era) vs barre chiuse;
  - passo 3% / 2% / 1.5% / 1%.

Uso:
  python -m jobs.grid_intraday_test [--giorni 180] [--finestra 30] [--hop 10]
                                    [--epic US100,GOLD] [--refresh]
I prezzi vengono messi in cache in data/cache/ (il demo e il reale hanno gli
stessi prezzi, si puo' lanciare in locale).
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.grid_net import ancora_mobile, target_isteresi, target_unita

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
EPICS = ["US100", "US30", "US500", "DE40", "NL25", "J225", "HK50", "GOLD"]
OVERNIGHT = 0.00030   # 0.03% del nozionale per notte e per unita' (SWAP reali:
                      # ~0.01€/notte su 30-45€ di nozionale)


def _arg(name, default):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def fetch_15m(capital, epic: str, giorni: int, refresh: bool) -> list[dict]:
    f = CACHE / f"15m_{epic}.json"
    if f.exists() and not refresh:
        d = json.loads(f.read_text())
        if d.get("giorni", 0) >= giorni:
            return d["barre"]
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=giorni)
    out, seen = [], set()
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=10))
        r = capital._session.get(
            capital._url(f"/prices/{epic}"), headers=capital._auth_headers(),
            params={"resolution": "MINUTE_15", "max": 1000,
                    "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                    "to": nxt.strftime("%Y-%m-%dT%H:%M:%S")}, timeout=30)
        if r.status_code != 200:
            print(f"  ! {epic} {cur.date()}..{nxt.date()}: HTTP {r.status_code} {r.text[:80]}")
        else:
            for x in r.json().get("prices", []):
                t = x.get("snapshotTimeUTC")
                cp = x.get("closePrice") or {}
                if t and t not in seen and cp.get("bid") and cp.get("ask"):
                    seen.add(t)
                    out.append({"t": t, "bid": float(cp["bid"]), "ask": float(cp["ask"])})
        cur = nxt
        time.sleep(0.25)
    out.sort(key=lambda b: b["t"])
    f.write_text(json.dumps({"giorni": giorni, "barre": out}))
    return out


def fetch_day(capital, epic: str, refresh: bool) -> list[dict]:
    f = CACHE / f"day_{epic}.json"
    if f.exists() and not refresh:
        return json.loads(f.read_text())
    px = capital.get_prices(epic, resolution="DAY", max_bars=400)
    out = []
    for x in px:
        cp = x.get("closePrice") or {}
        if cp.get("bid") and cp.get("ask"):
            out.append({"d": x["snapshotTimeUTC"][:10],
                        "c": (float(cp["bid"]) + float(cp["ask"])) / 2})
    f.write_text(json.dumps(out))
    return out


def nozionale_unita(capital, epic: str) -> float:
    """Euro di nozionale di UNA unita' (min size del broker), come nel job."""
    from src.executor import _market_meta
    from src.risk import quote_to_ref_factor
    f = CACHE / f"meta_{epic}.json"
    if f.exists():
        return float(json.loads(f.read_text())["noz"])
    mk = capital.get_market(epic)
    meta = _market_meta(mk, leverages_map=capital.get_leverages_map(), use_real_leverage=True)
    q2r = quote_to_ref_factor((mk.get("instrument") or {}).get("currency"), capital) or 1.0
    noz = meta["min_size"] * meta["mid_price"] * q2r
    f.write_text(json.dumps({"noz": noz, "min_size": meta["min_size"], "q2r": q2r}))
    return noz


def ema_per_giorno(day: list[dict], periodo: int) -> dict[str, float]:
    """EMA calcolata sulle barre CHIUSE fino al giorno precedente: ema[d] usa i
    close dei giorni < d."""
    out = {}
    closes = []
    for x in day:
        if closes:
            out[x["d"]] = ancora_mobile(closes, periodo)
        closes.append(x["c"])
    # anche per i giorni successivi all'ultima barra
    out["9999-99-99"] = ancora_mobile(closes, periodo)
    return out


def simula(barre: list[dict], ema_chiusa: dict, *, step: float, isteresi: bool,
           ema_corrente: bool, max_unita: int, periodo: int) -> dict:
    """Ritorna curva equity (in punti per 1 unita') e statistiche fill."""
    k = 2.0 / (periodo + 1.0)
    u = 0
    cash = 0.0
    costi_on = 0.0
    fills = 0
    giorno_prev = None
    curva = []          # (t, equity_punti)
    giri_val = []       # valore in punti dei round-trip (approssimato: cash delta al ritorno a 0)
    cash_at_zero = 0.0
    giorni_ema = sorted(ema_chiusa)
    import bisect
    for b in barre:
        d = b["t"][:10]
        mid = (b["bid"] + b["ask"]) / 2
        i = bisect.bisect_right(giorni_ema, d)
        # ema dei giorni < d: la chiave e' il primo giorno >= d
        key = giorni_ema[i - 1] if i and giorni_ema[i - 1] == d else (giorni_ema[i] if i < len(giorni_ema) else None)
        if key is None:
            continue
        p0 = ema_chiusa[key]
        if ema_corrente:
            p0 = mid * k + p0 * (1 - k)
        if giorno_prev is not None and d != giorno_prev and u != 0:
            costi_on += abs(u) * mid * OVERNIGHT
        giorno_prev = d
        tgt = (target_isteresi(mid, p0, step, u, max_unita) if isteresi
               else target_unita(mid, p0, step, max_unita))
        if tgt != u:
            delta = tgt - u
            px = b["ask"] if delta > 0 else b["bid"]
            cash -= delta * px
            u = tgt
            fills += 1
            if u == 0:
                giri_val.append(cash - cash_at_zero)
                cash_at_zero = cash
        curva.append((b["t"], cash + u * mid - costi_on))
    # chiusura finale a mercato
    if barre:
        mid = (barre[-1]["bid"] + barre[-1]["ask"]) / 2
    return {"curva": curva, "fills": fills, "costi_on": costi_on,
            "giri": giri_val, "finale": curva[-1][1] if curva else 0.0}


def finestre(barre, ema_chiusa, cfg, fin_gg, hop_gg, max_unita, periodo):
    """Simula finestre mobili indipendenti (posizione da zero a ogni finestra)."""
    if not barre:
        return []
    t0 = datetime.fromisoformat(barre[0]["t"])
    t1 = datetime.fromisoformat(barre[-1]["t"])
    out = []
    cur = t0
    while cur + timedelta(days=fin_gg) <= t1:
        end = cur + timedelta(days=fin_gg)
        sub = [b for b in barre if cur.isoformat() <= b["t"] < end.isoformat()]
        if len(sub) > 50:
            r = simula(sub, ema_chiusa, max_unita=max_unita, periodo=periodo, **cfg)
            p_in = (sub[0]["bid"] + sub[0]["ask"]) / 2
            out.append({"da": cur.date().isoformat(), "pct": r["finale"] / p_in * 100,
                        "fills": r["fills"], "costi_on_pct": r["costi_on"] / p_in * 100,
                        "giri": r["giri"], "p_in": p_in})
        cur += timedelta(days=hop_gg)
    return out


CONFIGS = {
    "A attuale: floor, EMA con barra corrente, 3%": dict(step=0.03, isteresi=False, ema_corrente=True),
    "G floor, EMA chiusa, 3%":                      dict(step=0.03, isteresi=False, ema_corrente=False),
    "B isteresi, EMA corrente, 3%":                 dict(step=0.03, isteresi=True, ema_corrente=True),
    "C isteresi, EMA chiusa, 3%":                   dict(step=0.03, isteresi=True, ema_corrente=False),
    "D isteresi, EMA chiusa, 2%":                   dict(step=0.02, isteresi=True, ema_corrente=False),
    "E isteresi, EMA chiusa, 1.5%":                 dict(step=0.015, isteresi=True, ema_corrente=False),
    "F isteresi, EMA chiusa, 1%":                   dict(step=0.01, isteresi=True, ema_corrente=False),
}


def main() -> int:
    giorni = int(_arg("--giorni", 180))
    fin = int(_arg("--finestra", 30))
    hop = int(_arg("--hop", 10))
    epics = _arg("--epic", ",".join(EPICS)).split(",")
    refresh = "--refresh" in sys.argv
    max_unita = int(_arg("--max-unita", 2))
    periodo = int(_arg("--ema", 5))

    from src.capital_client import CapitalClient
    CACHE.mkdir(parents=True, exist_ok=True)
    cap = CapitalClient(load_config())
    cap.login()

    dati = {}
    for ep in epics:
        b = fetch_15m(cap, ep, giorni, refresh)
        d = fetch_day(cap, ep, refresh)
        noz = nozionale_unita(cap, ep)
        dati[ep] = (b, ema_per_giorno(d, periodo), noz)
        print(f"{ep}: {len(b)} barre 15m dal {b[0]['t'][:10] if b else '?'}, "
              f"{len(d)} barre DAY, unita' = {noz:.1f}€")

    print(f"\nFinestre di {fin} giorni, hop {hop}, max {max_unita} unita', EMA{periodo}, "
          f"overnight {OVERNIGHT*100:.3f}%/notte. Euro alla TAGLIA REALE (1 unita' = min size).\n")
    riepilogo = []
    per_epic = {}
    for nome, cfg in CONFIGS.items():
        tutte = []
        for ep, (b, ema, noz) in dati.items():
            ws = finestre(b, ema, cfg, fin, hop, max_unita, periodo)
            for w in ws:
                w["eur"] = w["pct"] / 100 * noz
                w["costi_eur"] = w["costi_on_pct"] / 100 * noz
                w["giri_eur"] = [g / w["p_in"] * noz for g in w["giri"]]
                w["epic"] = ep
            tutte += ws
            per_epic.setdefault(ep, {})[nome] = ws
        if not tutte:
            continue
        n = len(tutte)
        pos = sum(w["eur"] > 0 for w in tutte)
        media = sum(w["eur"] for w in tutte) / n
        peggio = min(w["eur"] for w in tutte)
        meglio = max(w["eur"] for w in tutte)
        fills_gg = sum(w["fills"] for w in tutte) / (n * fin)
        giri = [g for w in tutte for g in w["giri_eur"]]
        giro_medio = sum(giri) / len(giri) if giri else 0.0
        costi = sum(w["costi_eur"] for w in tutte) / n
        riepilogo.append((nome, n, pos / n * 100, media, peggio, meglio, fills_gg, giro_medio, costi, len(giri) / n))
    print(f"{'configurazione':46s} {'fin.':>4s} {'%pos':>5s} {'media€':>7s} {'peggio€':>8s} {'meglio€':>8s} "
          f"{'fill/gg':>7s} {'giri/fin':>8s} {'€/giro':>7s} {'on€/fin':>8s}")
    for r in riepilogo:
        print(f"{r[0]:46s} {r[1]:4d} {r[2]:5.0f} {r[3]:+7.2f} {r[4]:+8.2f} {r[5]:+8.2f} "
              f"{r[6]:7.2f} {r[9]:8.1f} {r[7]:+7.3f} {r[8]:8.2f}")

    print("\nPer strumento (media €/finestra, %finestre positive):")
    nomi = list(CONFIGS)
    print(f"{'epic':6s} " + " ".join(f"{n[:1]:>12s}" for n in nomi))
    for ep in dati:
        cells = []
        for n in nomi:
            ws = per_epic.get(ep, {}).get(n, [])
            if ws:
                m = sum(w["eur"] for w in ws) / len(ws)
                p = sum(w["eur"] > 0 for w in ws) / len(ws) * 100
                cells.append(f"{m:+6.2f}/{p:3.0f}%")
            else:
                cells.append(f"{'n/d':>12s}")
        print(f"{ep:6s} " + " ".join(f"{c:>12s}" for c in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
