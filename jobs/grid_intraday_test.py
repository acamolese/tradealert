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
                                    [--epic US100,GOLD] [--refresh] [--res MINUTE_5]
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
from src.grid_net import (ancora_mobile, target_banda, target_isteresi,
                          target_unita, target_volatilita, unita_da_rischio)

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
EPICS = ["US100", "US30", "US500", "DE40", "NL25", "J225", "HK50", "GOLD"]
OVERNIGHT = 0.00030   # 0.03% del nozionale per notte e per unita' (SWAP reali:
                      # ~0.01€/notte su 30-45€ di nozionale)


def _arg(name, default):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def fetch_15m(capital, epic: str, giorni: int, refresh: bool,
              res: str = "MINUTE_15") -> list[dict]:
    tag = {"MINUTE_15": "15m", "MINUTE_5": "5m", "MINUTE_30": "30m", "HOUR": "1h"}.get(res, res)
    f = CACHE / f"{tag}_{epic}.json"
    if f.exists() and not refresh:
        d = json.loads(f.read_text())
        if d.get("giorni", 0) >= giorni:
            return d["barre"]
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=giorni)
    out, seen = [], set()
    cur = start
    while cur < end:
        passo_gg = {"MINUTE_5": 3, "MINUTE_15": 10}.get(res, 10)
        nxt = min(end, cur + timedelta(days=passo_gg))
        r = capital._session.get(
            capital._url(f"/prices/{epic}"), headers=capital._auth_headers(),
            params={"resolution": res, "max": 1000,
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


def simula(barre: list[dict], ema_chiusa: dict, *, step: float = 0.03,
           isteresi: bool = False, ema_corrente: bool = True, max_unita: int = 2,
           periodo: int = 5, banda: tuple | None = None,
           sempre_long: bool = False, vol: tuple | None = None,
           ancora_ore: int = 0, rischio: float = 0.0, centro: int = 0,
           chiudi_la_notte: bool = False, peso_vol: float = 0.0) -> dict:
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
    # ancoraggio corto e volatilita' sulla stessa scala: servono a vol/ancora_ore
    n_corte = max(2, ancora_ore * 4) if ancora_ore else (32 if peso_vol else 0)
    finestra_px: list[float] = []
    p0_corta = None
    peso_corr = None
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
        if n_corte:
            finestra_px.append(mid)
            if len(finestra_px) > n_corte * 3:
                finestra_px.pop(0)
            k2 = 2.0 / (n_corte + 1.0)
            p0_corta = mid * k2 + (p0_corta if p0_corta else mid) * (1 - k2)
        sigma = 0.0
        if vol and len(finestra_px) > n_corte:
            r = [abs(finestra_px[j] / finestra_px[j - 1] - 1)
                 for j in range(1, len(finestra_px))]
            sigma = (sum(r) / len(r)) * (n_corte ** 0.5) if r else 0.0

        if sempre_long:
            tgt = 1                      # pietra di paragone: comprato e tenuto
        elif vol:
            rif = p0_corta if p0_corta else p0
            cap_u = max_unita
            if rischio:
                cap_u = max(1, int(round(unita_da_rischio(sigma, rischio, max_unita))))
            tgt = target_volatilita(mid, rif, sigma, vol[0], vol[1], vol[2], u, cap_u)
        elif banda:
            tgt = target_banda(mid, p0, banda[0], banda[1], banda[2], u, max_unita)
        elif isteresi:
            tgt = target_isteresi(mid, p0, step, u, max_unita)
        else:
            tgt = target_unita(mid, p0, step, max_unita)
        # Niente posizioni quando il broker addebita il finanziamento (21:00 UTC).
        # Misurato: evitare una notte risparmia lo 0,0216% sugli indici USA e
        # costa uno spread, che a quell'ora il broker allarga ma non abbastanza.
        if centro and not sempre_long:
            # il grid non oscilla piu' attorno allo zero ma attorno a una
            # posizione lunga: cattura la deriva del mercato E le oscillazioni
            tgt = max(-max_unita, min(max_unita, tgt + centro))
        # Niente posizioni quando il broker addebita il finanziamento (21:00 UTC).
        # Va applicata per ultima, altrimenti il centro la annulla subito.
        if chiudi_la_notte and (int(b["t"][11:13]) >= 20 or int(b["t"][11:13]) < 7):
            tgt = 0
        if peso_vol and len(finestra_px) > 20:
            # esposizione inversamente proporzionale a quanto il mercato si
            # muove. Il peso si aggiorna solo quando cambia di piu' di un
            # quarto: ribilanciare a ogni barra costa 63 operazioni al giorno
            # e le spese si mangiano tutto (misurato).
            r = [abs(finestra_px[j] / finestra_px[j - 1] - 1)
                 for j in range(1, len(finestra_px))]
            sig = (sum(r) / len(r)) if r else 0.0
            if sig > 0:
                # peso_vol negativo = esposizione PROPORZIONALE alla volatilita':
                # coerente col premio al rischio misurato sul VIX (piu' paura,
                # piu' rendimento atteso), opposto alla gestione del rischio classica
                nuovo = (max(0.2, min(3.0, sig / abs(peso_vol))) if peso_vol < 0
                         else max(0.2, min(3.0, peso_vol / sig)))
                if peso_corr is None or abs(nuovo / peso_corr - 1) > 0.25:
                    peso_corr = nuovo
            if peso_corr:
                tgt = tgt * peso_corr
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
    # Banda simmetrica con zona morta, passo e isteresi separati (2026-09-07):
    # (entrata, uscita, passo). Nasce dall'osservazione che in 28.774
    # rilevamenti il sistema non e' mai andato short.
    "H banda 0.50 / 0.10, passo 0.50%":             dict(banda=(0.005, 0.001, 0.005)),
    "I banda 0.75 / 0.10, passo 0.75%":             dict(banda=(0.0075, 0.001, 0.0075)),
    "L banda 1.00 / 0.10, passo 1.00%":             dict(banda=(0.010, 0.001, 0.010)),
    "M banda 0.75 / 0.25, passo 0.75%":             dict(banda=(0.0075, 0.0025, 0.0075)),
    "N banda 0.75 / 0.10, EMA chiusa":              dict(banda=(0.0075, 0.001, 0.0075), ema_corrente=False),
    "Z riferimento: comprato e tenuto":             dict(sempre_long=True),
    # Calibrate sulla finestra dove il segnale batte il costo (2026-09-07):
    # ancoraggio a poche ore invece che 5 giorni, soglie in multipli della
    # volatilita' corrente invece che al 3% fisso.
    "P 4h, soglie 1.5 sigma":     dict(vol=(1.5, 0.2, 1.5), ancora_ore=4),
    "Q 4h, soglie 1.0 sigma":     dict(vol=(1.0, 0.2, 1.0), ancora_ore=4),
    "R 8h, soglie 1.5 sigma":     dict(vol=(1.5, 0.2, 1.5), ancora_ore=8),
    "S 8h, soglie 2.0 sigma":     dict(vol=(2.0, 0.3, 2.0), ancora_ore=8),
    "T 4h, 1.5 sigma + taglia a rischio costante":
                                  dict(vol=(1.5, 0.2, 1.5), ancora_ore=4, rischio=0.006),
    "U 8h, 1.5 sigma + taglia a rischio costante":
                                  dict(vol=(1.5, 0.2, 1.5), ancora_ore=8, rischio=0.006),
    # Il grid attorno a una posizione lunga invece che attorno allo zero:
    # prende la deriva del mercato come il comprato-e-tenuto, e in piu' compra
    # sui cali e alleggerisce sui rialzi.
    "V attuale 3% attorno a +1":  dict(step=0.03, isteresi=False, ema_corrente=True, centro=1),
    "W isteresi 3% attorno a +1": dict(step=0.03, isteresi=True, ema_corrente=True, centro=1),
    "X 8h 2 sigma attorno a +1":  dict(vol=(2.0, 0.3, 2.0), ancora_ore=8, centro=1),
    "Y banda 0.75 attorno a +1":  dict(banda=(0.0075, 0.001, 0.0075), centro=1),
    # Senza posizioni nella notte: non paga il finanziamento, paga uno spread
    "AA attuale, ma chiude la notte":   dict(step=0.03, isteresi=False, ema_corrente=True,
                                             chiudi_la_notte=True),
    "AB attorno a +1, chiude la notte": dict(step=0.03, isteresi=False, ema_corrente=True,
                                             centro=1, chiudi_la_notte=True),
    "AC 8h 2 sigma, chiude la notte":   dict(vol=(2.0, 0.3, 2.0), ancora_ore=8,
                                             chiudi_la_notte=True),
    # Esposizione dosata sulla volatilita', in continuo: si riduce quando il
    # mercato e' agitato e si alza quando e' calmo. Non prevede la direzione.
    "BA comprato e tenuto, dosato":  dict(sempre_long=True, peso_vol=0.0008),
    "BB attuale, dosato":            dict(step=0.03, isteresi=False, ema_corrente=True,
                                          peso_vol=0.0008),
    "BC attorno a +1, dosato":       dict(step=0.03, isteresi=False, ema_corrente=True,
                                          centro=1, peso_vol=0.0008),
    "BD comprato e tenuto, dosato forte": dict(sempre_long=True, peso_vol=0.0005),
    # L'opposto: piu' il mercato e' agitato, piu' si sta esposti
    "CA comprato e tenuto, esposto alla paura": dict(sempre_long=True, peso_vol=-0.0008),
    "CB attuale + esposto alla paura":  dict(step=0.03, isteresi=False, ema_corrente=True,
                                             peso_vol=-0.0008),
    "CC attorno a +1 + esposto alla paura": dict(step=0.03, isteresi=False,
                                                 ema_corrente=True, centro=1,
                                                 peso_vol=-0.0008),
}


def main() -> int:
    giorni = int(_arg("--giorni", 180))
    fin = int(_arg("--finestra", 30))
    hop = int(_arg("--hop", 10))
    epics = _arg("--epic", ",".join(EPICS)).split(",")
    refresh = "--refresh" in sys.argv
    max_unita = int(_arg("--max-unita", 2))
    periodo = int(_arg("--ema", 5))
    res = _arg("--res", "MINUTE_15")

    from src.capital_client import CapitalClient
    CACHE.mkdir(parents=True, exist_ok=True)
    cap = CapitalClient(load_config())
    cap.login()

    dati = {}
    for ep in epics:
        b = fetch_15m(cap, ep, giorni, refresh, res)
        d = fetch_day(cap, ep, refresh)
        noz = nozionale_unita(cap, ep)
        dati[ep] = (b, ema_per_giorno(d, periodo), noz)
        print(f"{ep}: {len(b)} barre {res} dal {b[0]['t'][:10] if b else '?'}, "
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
