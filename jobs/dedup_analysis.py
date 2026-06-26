"""Analisi cost/benefit del dedup 24h (pre-registrata in
docs/sprint5-dedup-analysis.md). Sola lettura.

Identifica i "setup azionabili bloccati dal dedup" (top proposta score>=7.0 in
uno scan no_setup, con (asset,direzione) gia' segnalata nelle 24h precedenti) e
ne ricostruisce l'esito che non c'e' stato: entry al prezzo dello scan, SL/TP =
mediana stop/target dei signal reali, first-touch su candele forward fino a 48h.
Aggrega: % winner e R medio dei setup bloccati.
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime, timezone, timedelta

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient

EPIC = {"Gold": "GOLD", "Brent Oil": "OIL_BRENT", "US500": "US500",
        "Nasdaq 100": "US100", "Bitcoin": "BTCUSD", "EUR/USD": "EURUSD",
        "AUD/USD": "AUDUSD", "GBP/USD": "GBPUSD", "Copper": "COPPER",
        "Hang Seng": "HK50", "Nikkei": "J225"}
HORIZON_H = 48
START = "2026-06-12"


def _naive(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _candles(cl, epic, frm, to):
    for res in ("MINUTE_15", "HOUR", "MINUTE_5"):
        try:
            r = cl._session.get(cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                                params={"resolution": res, "from": _naive(frm),
                                        "to": _naive(to), "max": 1000}, timeout=25)
            if r.status_code == 200 and r.json().get("prices"):
                return r.json()["prices"], res
        except Exception:
            pass
    return [], None


def _mid(p, side):
    return (p[side]["bid"] + p[side]["ask"]) / 2


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    sigs_all = (db._client.table("signals").select("asset,direction,stop_loss,take_profit,created_at")
                .gte("created_at", "2026-05-15").order("created_at").execute().data)
    stops = [float(s["stop_loss"]) for s in sigs_all if s.get("stop_loss")]
    targets = [float(s["take_profit"]) for s in sigs_all if s.get("take_profit")]
    med_stop = round(statistics.median(stops), 2)
    med_target = round(statistics.median(targets), 2)
    print(f"mediana reale: stop={med_stop}% target={med_target}% (R:R {med_target/med_stop:.2f})")

    runs = (db._client.table("scanner_runs").select("ran_at,outcome,notes")
            .gte("ran_at", START).order("ran_at").execute().data)
    # indice signal per dedup-lookback
    sig_times = [(s["asset"], s["direction"],
                  datetime.fromisoformat(s["created_at"])) for s in sigs_all]

    blocked = []
    for r in runs:
        if r.get("outcome") != "no_setup":
            continue
        props = (r.get("notes") or {}).get("proposals") or []
        elig = [p for p in props if (p.get("direction") in ("long", "short")) and (p.get("score") or 0) >= 7.0]
        if not elig:
            continue
        top = max(elig, key=lambda p: p.get("score") or 0)
        asset = top.get("''") or top.get("asset")
        direction = top.get("direction")
        ran = datetime.fromisoformat(r["ran_at"])
        # (asset,dir) gia' segnalata nelle 24h precedenti questo scan?
        recent = any(a == asset and d == direction and (ran - timedelta(hours=24)) <= t < ran
                     for a, d, t in sig_times)
        if recent and asset in EPIC:
            blocked.append((asset, direction, top.get("score"), ran))

    print(f"setup azionabili bloccati dal dedup (score>=7, recente): {len(blocked)}")
    results = []
    for asset, direction, score, ran in blocked[:60]:
        epic = EPIC[asset]
        ran_utc = ran.astimezone(timezone.utc).replace(tzinfo=None)
        c, res = _candles(cl, epic, ran_utc - timedelta(minutes=30), ran_utc + timedelta(hours=HORIZON_H))
        time.sleep(0.3)
        if not c:
            continue
        # entry = primo close >= ran
        entry = None
        fwd = []
        for p in c:
            t = datetime.fromisoformat(p["snapshotTimeUTC"])
            if t >= ran_utc:
                if entry is None:
                    entry = _mid(p, "openPrice")
                fwd.append(p)
        if entry is None or not fwd:
            continue
        r_dist = entry * med_stop / 100
        reward = entry * med_target / 100
        outcome_R = None
        for p in fwd:
            if direction == "short":
                hi = _mid(p, "highPrice"); lo = _mid(p, "lowPrice")
                if hi >= entry + r_dist:  # SL (per short SL sopra)
                    outcome_R = -1.0; break
                if lo <= entry - reward:  # TP
                    outcome_R = reward / r_dist; break
            else:
                lo = _mid(p, "lowPrice"); hi = _mid(p, "highPrice")
                if lo <= entry - r_dist:
                    outcome_R = -1.0; break
                if hi >= entry + reward:
                    outcome_R = reward / r_dist; break
        if outcome_R is None:  # ne' SL ne' TP entro 48h: close finale
            last = _mid(fwd[-1], "closePrice")
            outcome_R = ((entry - last) if direction == "short" else (last - entry)) / r_dist
        results.append((asset, direction, score, round(outcome_R, 2)))

    print(f"\n=== ESITI simulati dei {len(results)} bloccati ===")
    by_asset = {}
    for a, d, s, R in results:
        by_asset.setdefault((a, d), []).append(R)
    for (a, d), Rs in sorted(by_asset.items()):
        print(f"  {a:<10} {d:<5} n={len(Rs):2d} R medio {statistics.mean(Rs):+.2f} (win {sum(1 for x in Rs if x>0)}/{len(Rs)})")
    if results:
        allR = [R for *_, R in results]
        win = sum(1 for R in allR if R > 0)
        print(f"\n  TOTALE: n={len(allR)} | win {win}/{len(allR)} ({100*win/len(allR):.0f}%) | R MEDIO {statistics.mean(allR):+.3f}")
        print("  criterio: >+0.2 dedup costa | <-0.2 dedup protegge | in mezzo neutro")
    return 0


if __name__ == "__main__":
    sys.exit(main())
