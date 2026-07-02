"""Walk-forward delle strategie candidate senza LLM (Sprint 7 M4).

Tre famiglie di ENTRY deterministiche; le USCITE sono il sistema live gia'
validato (M3): SL iniziale a k*ATR, TP a rr*SL, trailing D+V1+V2.

  donchian  - breakout del massimo/minimo degli ultimi N giorni
  emacross  - incrocio EMA veloce/lenta (calcolate su barre orarie, finestre
              espresse in giorni), entra al cross nella direzione del trend
  tsmom     - momentum time-series: segno del rendimento a K giorni, con
              soglia minima 1 ATR per evitare il rumore

Protocollo pre-registrato (docs/sprint7-rethink.md M4):
- in-sample 2020-01..2023-12: si sceglie UNA config per famiglia
  (max R totale netto, vincolo n>=40);
- out-of-sample 2024-01..oggi: giudizio SOLO qui, sulla config scelta;
- GATE per candidata: exp OOS >= +0.20R netto costi, n>=60, senza i 2
  migliori trade >= +0.10R, positiva in almeno 2 dei 3 terzi del periodo OOS.

Costi inclusi: spread reale (entry long all'ask, short al bid; uscite
speculari), overnight financing stimato per notte di calendario
(0.025%/notte non-crypto, 0.06%/notte BTC, sul nozionale).
Cooldown 24h dopo ogni uscita (come il dedup live). Max holding 30 giorni.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/backtest_run.py
"""

from __future__ import annotations

import csv
import gzip
import statistics
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from jobs.monitor_close_replay import make_offset_fn

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "candles"
EPICS = ["GOLD", "OIL_BRENT", "US500", "US100", "BTCUSD"]
OVERNIGHT_PCT = {"BTCUSD": 0.06, "ETHUSD": 0.06}  # default 0.025 sotto
OVERNIGHT_DEFAULT = 0.025
# CLI: --res DAY|HOUR, --is-end YYYY-MM-DD, --require-is-positive, --epics A,B
RES = "HOUR"
IS_END = datetime(2024, 1, 1)
REQUIRE_IS_POSITIVE = False
ATR_BARS_BY_RES = {"HOUR": 56, "DAY": 14}  # ~14 periodi 4H / ATR14 classico
COOLDOWN_H = 24
MAX_HOLD_DAYS = 30
GATE = {"exp": 0.20, "n": 60, "drop2": 0.10}

OFFSET_FN = make_offset_fn(True, True)  # trailing live: D + V1 + V2


def load(epic):
    atr_bars = ATR_BARS_BY_RES[RES]
    ts, ob, oa, hb, ha, lb, la, cb, ca = [], [], [], [], [], [], [], [], []
    with gzip.open(DATA_DIR / f"{epic}_{RES}.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            try:
                row = [float(r[k]) for k in ("open_bid", "open_ask", "high_bid",
                       "high_ask", "low_bid", "low_ask", "close_bid", "close_ask")]
            except (ValueError, TypeError):
                continue
            ts.append(datetime.fromisoformat(r["ts"]))
            for lst, v in zip((ob, oa, hb, ha, lb, la, cb, ca), row):
                lst.append(v)
    mid_h = [(a + b) / 2 for a, b in zip(hb, ha)]
    mid_l = [(a + b) / 2 for a, b in zip(lb, la)]
    mid_c = [(a + b) / 2 for a, b in zip(cb, ca)]
    # ATR (SMA del true range su atr_bars barre)
    atr = [None] * len(ts)
    trs = deque(maxlen=atr_bars)
    for i in range(len(ts)):
        prev_c = mid_c[i - 1] if i else mid_c[0]
        tr = max(mid_h[i] - mid_l[i], abs(mid_h[i] - prev_c), abs(prev_c - mid_l[i]))
        trs.append(tr)
        if len(trs) == atr_bars:
            atr[i] = sum(trs) / atr_bars
    return {"ts": ts, "ob": ob, "oa": oa, "hb": hb, "ha": ha, "lb": lb,
            "la": la, "cb": cb, "ca": ca, "mc": mid_c, "atr": atr, "n": len(ts)}


def ema_series(vals, days, ts):
    # EMA su barre orarie con periodo espresso in giorni: alpha da barre/giorno mediane
    if len(ts) < 100:
        return [None] * len(vals)
    per_day = len(ts) / max((ts[-1] - ts[0]).days, 1)
    n = max(int(days * per_day), 2)
    alpha = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else e + alpha * (v - e)
        out.append(e)
    return out


def gen_signals(d, family, p):
    """Ritorna lista (indice_barra, direzione) valutata sul CLOSE della barra."""
    ts, mc, atr = d["ts"], d["mc"], d["atr"]
    sigs = []
    if family == "donchian":
        look = timedelta(days=p["N"])
        mh = [(a + b) / 2 for a, b in zip(d["hb"], d["ha"])]
        ml = [(a + b) / 2 for a, b in zip(d["lb"], d["la"])]
        hi, lo = deque(), deque()   # monotone: sliding-window max/min per tempo
        t0 = ts[0]
        for i in range(d["n"]):
            t = ts[i]
            while hi and hi[0][0] < t - look:
                hi.popleft()
            while lo and lo[0][0] < t - look:
                lo.popleft()
            # segnale sul close, canale sui massimi/minimi PRECEDENTI
            if hi and atr[i] and t - t0 > look:
                if mc[i] > hi[0][1]:
                    sigs.append((i, "long"))
                elif mc[i] < lo[0][1]:
                    sigs.append((i, "short"))
            while hi and hi[-1][1] <= mh[i]:
                hi.pop()
            hi.append((t, mh[i]))
            while lo and lo[-1][1] >= ml[i]:
                lo.pop()
            lo.append((t, ml[i]))
        return sigs
    if family == "emacross":
        f = ema_series(mc, p["fast"], ts)
        s = ema_series(mc, p["slow"], ts)
        warm = int(p["slow"] * (d["n"] / max((ts[-1] - ts[0]).days, 1)))
        for i in range(max(warm, 1), d["n"]):
            if atr[i] is None:
                continue
            if f[i - 1] <= s[i - 1] and f[i] > s[i]:
                sigs.append((i, "long"))
            elif f[i - 1] >= s[i - 1] and f[i] < s[i]:
                sigs.append((i, "short"))
        return sigs
    if family == "tsmom":
        look = timedelta(days=p["K"])
        past = deque()  # (ts, close)
        for i in range(d["n"]):
            t = ts[i]
            past.append((t, mc[i]))
            while past and past[0][0] < t - look:
                past.popleft()
            if atr[i] is None or (t - past[0][0]) < look * 0.9:
                continue
            ret = mc[i] - past[0][1]
            if abs(ret) > 1.0 * atr[i] * (look.days ** 0.5):
                sigs.append((i, "long" if ret > 0 else "short"))
        return sigs
    raise ValueError(family)


def run_config(d, epic, family, p):
    """Esegue i segnali in sequenza (una posizione per asset, cooldown 24h)."""
    sigs = gen_signals(d, family, p)
    trades = []
    busy_until = None   # ts fine trade + cooldown
    max_hold = timedelta(days=MAX_HOLD_DAYS)
    on_pct = OVERNIGHT_PCT.get(epic, OVERNIGHT_DEFAULT)
    for i, direction in sigs:
        t = d["ts"][i]
        if busy_until and t < busy_until:
            continue
        if i + 1 >= d["n"] or d["atr"][i] is None:
            continue
        j = i + 1
        entry = d["oa"][j] if direction == "long" else d["ob"][j]
        r_dist = p["sl_atr"] * d["atr"][i]
        reward = p["rr"] * r_dist
        rr = p["rr"]
        # simulazione uscita dalla barra j in poi
        peak_r, peak_fav = 0.0, 0.0
        exit_r, exit_ts = None, None
        horizon = t + max_hold
        for k in range(j, d["n"]):
            if d["ts"][k] > horizon:
                px = d["ca"][k - 1] if direction == "short" else d["cb"][k - 1]
                exit_r = ((entry - px) if direction == "short" else (px - entry)) / r_dist
                exit_ts = d["ts"][k - 1]
                break
            frac_tp = peak_fav / reward if reward else None
            off = OFFSET_FN(peak_r, frac_tp, rr)
            if direction == "short":
                sl_price = entry - off * r_dist
                tp_price = entry - reward
                if d["ha"][k] >= sl_price:
                    exit_r, exit_ts = off, d["ts"][k]
                    break
                if d["la"][k] <= tp_price:
                    exit_r, exit_ts = rr, d["ts"][k]
                    break
                fav = entry - d["la"][k]
            else:
                sl_price = entry + off * r_dist
                tp_price = entry + reward
                if d["lb"][k] <= sl_price:
                    exit_r, exit_ts = off, d["ts"][k]
                    break
                if d["hb"][k] >= tp_price:
                    exit_r, exit_ts = rr, d["ts"][k]
                    break
                fav = d["hb"][k] - entry
            if fav > peak_fav:
                peak_fav = fav
                peak_r = fav / r_dist
        if exit_r is None:      # dati finiti col trade aperto: scarta
            break
        nights = (exit_ts.date() - t.date()).days
        fee_r = nights * (on_pct / 100.0) * entry / r_dist
        trades.append({"entry_ts": t, "exit_ts": exit_ts, "dir": direction,
                       "epic": epic, "r_net": exit_r - fee_r, "r_gross": exit_r,
                       "fee_r": fee_r})
        busy_until = exit_ts + timedelta(hours=COOLDOWN_H)
    return trades


CONFIGS = {
    "donchian": [{"N": N, "sl_atr": s, "rr": r}
                 for N in (10, 20, 40) for s in (1.5, 2.5) for r in (2.0, 3.0)],
    "emacross": [{"fast": f, "slow": sl, "sl_atr": s, "rr": r}
                 for f, sl in ((10, 40), (20, 80)) for s in (1.5, 2.5) for r in (2.0, 3.0)],
    "tsmom": [{"K": K, "sl_atr": s, "rr": r}
              for K in (20, 60) for s in (1.5, 2.5) for r in (2.0, 3.0)],
}


def summarize(trades):
    if not trades:
        return {"n": 0, "exp": float("nan"), "tot": 0.0}
    rs = [t["r_net"] for t in trades]
    return {"n": len(rs), "exp": statistics.mean(rs), "tot": sum(rs),
            "win": sum(1 for r in rs if r > 0) / len(rs),
            "fee": statistics.mean(t["fee_r"] for t in trades)}


def main() -> int:
    global RES, IS_END, REQUIRE_IS_POSITIVE, EPICS
    args = sys.argv[1:]
    if "--res" in args:
        RES = args[args.index("--res") + 1]
    if "--is-end" in args:
        IS_END = datetime.fromisoformat(args[args.index("--is-end") + 1])
    if "--require-is-positive" in args:
        REQUIRE_IS_POSITIVE = True
    if "--epics" in args:
        EPICS = args[args.index("--epics") + 1].split(",")
    print(f"res={RES} IS fino a {IS_END.date()} require_is_positive={REQUIRE_IS_POSITIVE}")
    data = {e: load(e) for e in EPICS}
    for e in EPICS:
        print(f"{e}: {data[e]['n']} barre {data[e]['ts'][0].date()} -> {data[e]['ts'][-1].date()}")

    results = {}
    for family, cfgs in CONFIGS.items():
        print(f"\n=== {family}: selezione in-sample (2020-2023) ===")
        best = None
        for p in cfgs:
            is_tr, oos_tr = [], []
            for e in EPICS:
                for t in run_config(data[e], e, family, p):
                    (is_tr if t["entry_ts"] < IS_END else oos_tr).append(t)
            s = summarize(is_tr)
            label = ",".join(f"{k}={v}" for k, v in p.items())
            print(f"  {label:<32} IS n={s['n']:>4} exp={s['exp']:+.3f} tot={s['tot']:+8.1f}")
            eligible = s["n"] >= 40 and (not REQUIRE_IS_POSITIVE or s["exp"] > 0)
            if eligible and (best is None or s["tot"] > best[1]["tot"]):
                best = (p, s, oos_tr)
        if best is None:
            print("  nessuna config con n>=40 in-sample")
            continue
        p, s_is, oos = best
        results[family] = (p, s_is, oos)

    print("\n" + "=" * 70)
    print("=== GIUDIZIO OUT-OF-SAMPLE (2024-01 -> oggi), config scelte IS ===")
    for family, (p, s_is, oos) in results.items():
        s = summarize(oos)
        label = ",".join(f"{k}={v}" for k, v in p.items())
        print(f"\n{family} [{label}]")
        print(f"  IS : n={s_is['n']} exp={s_is['exp']:+.3f}R")
        if not oos:
            print("  OOS: nessun trade")
            continue
        print(f"  OOS: n={s['n']} exp={s['exp']:+.3f}R tot={s['tot']:+.1f}R "
              f"win={s['win']:.0%} fee medio={s['fee']:.3f}R")
        rs = sorted((t["r_net"] for t in oos), reverse=True)
        drop2 = statistics.mean(rs[2:]) if len(rs) > 2 else float("nan")
        third = max(len(oos) // 3, 1)
        oos_sorted = sorted(oos, key=lambda t: t["entry_ts"])
        terzi = [sum(t["r_net"] for t in oos_sorted[k * third:(k + 1) * third if k < 2 else len(oos)])
                 for k in range(3)]
        pos_terzi = sum(1 for x in terzi if x > 0)
        print(f"  robustezza: senza top2 exp={drop2:+.3f}R | terzi R={['%+.1f' % x for x in terzi]} "
              f"({pos_terzi}/3 positivi)")
        ok = (s["exp"] >= GATE["exp"] and s["n"] >= GATE["n"]
              and drop2 >= GATE["drop2"] and pos_terzi >= 2)
        print(f"  >>> GATE M4: {'CANDIDATA VALIDA' if ok else 'NON PASSA'} <<<")
        # breakdown per asset e direzione
        for e in EPICS:
            sub = [t for t in oos if t["epic"] == e]
            if sub:
                print(f"    {e:<10} n={len(sub):>3} exp={statistics.mean(x['r_net'] for x in sub):+.3f}R")
        for dd in ("long", "short"):
            sub = [t for t in oos if t["dir"] == dd]
            if sub:
                print(f"    {dd:<10} n={len(sub):>3} exp={statistics.mean(x['r_net'] for x in sub):+.3f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
