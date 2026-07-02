"""Replay controfattuale delle chiusure del monitor LLM (Sprint 6 A1, gate pre-registrato).

Per ogni trade chiuso dal monitor (proposta accettata o auto-close) ricostruisce
la traiettoria dalle candele Capital (5m, fallback 15m/30m) e simula cosa sarebbe
successo lasciando il trade al solo bracket SL trailato / TP, proseguendo OLTRE
il closed_at reale fino a uscita bracket o cap di 10 giorni di calendario.

Assunzioni dichiarate (pre-registrate in docs/sprint6-piano-scalata.md):
- regola di trailing = quella in vigore alla data di APERTURA del trade:
  D pura (< 2026-06-10), D+V1 (< 2026-06-22), D+V1+V2 (>= 2026-06-22);
- short si chiude all'ask, long al bid (come jobs/trailing_calibration.py);
- se in una stessa candela vengono colpiti sia SL sia TP, si assume SL
  (conservativo, a favore del monitor);
- all'orizzonte (closed_at + 10gg) trade ancora aperto -> mark-to-market
  sull'ultima candela, caso conteggiato a parte;
- exit_R reale calcolato da close_price (fallback pnl/rischio se assente);
- i weekend restano nel replay: il controfattuale paga i gap.

Metrica primaria del gate: delta_r = exit_r_controfattuale - exit_r_reale.
Verdetto DANNOSO se media >= +0.15R, robusto (>= +0.10R senza i 2 migliori
controfattuali) e mediana >= 0. UTILE se speculare negativo. Altrimenti NEUTRO.

Sola lettura. Serve sessione Capital (credenziali nel .env) + chiave DB.
Uso: PYTHONPATH=$PWD .venv/bin/python jobs/monitor_close_replay.py
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient

HORIZON_DAYS = 10
MIN_SAMPLE = 25  # sotto questa soglia il gate si ferma (pre-registrato)
V1_LIVE = datetime(2026, 6, 10)
V2_LIVE = datetime(2026, 6, 22)
PREFIXES = ("manual:Richiesta utente da monitor", "manual:auto-close LLM monitor")

# fallback per trade senza epic sul signal
EPIC_FALLBACK = {"Gold": "GOLD", "Brent Oil": "OIL_BRENT", "US500": "US500",
                 "Nasdaq 100": "US100", "Bitcoin": "BTCUSD", "Copper": "COPPER",
                 "Hang Seng": "HK50", "Nikkei 225": "J225", "US Tech 100": "US100",
                 "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "AUD/USD": "AUDUSD"}

# --- replica trailing di src/position_monitor.py (D + V1 + V2 + lock TP) ---
_STEP = 0.25
_TP_LOCKS = ((0.90, 0.65), (0.80, 0.45))


def _granular(profit_r: float) -> float:
    if profit_r < 1.0:
        return -0.5 + math.floor((profit_r - 0.5) / _STEP) * _STEP
    return math.floor((profit_r - 1.0) / _STEP) * _STEP


def _v1_lowband(profit_r: float) -> float:
    if profit_r < 0.75:
        return -0.25 + (profit_r - 0.5) / 0.25 * 0.15
    return -0.10 + (profit_r - 0.75) / 0.25 * 0.10


def _v2_highband(profit_r: float) -> float:
    return 0.0 + (profit_r - 1.0) / 0.25 * 0.25


def _tp_lock(frac_tp, rr):
    if frac_tp is None or rr is None:
        return None
    for frac_min, lock_frac in _TP_LOCKS:
        if frac_tp >= frac_min:
            return lock_frac * rr
    return None


def make_offset_fn(v1: bool, v2: bool):
    def offset(peak_r, frac_tp, rr):
        if peak_r < 0.5:
            return -1.0  # nessun trailing sotto 0.5R: SL iniziale
        if v1 and 0.5 <= peak_r < 1.0:
            off = _v1_lowband(peak_r)
        elif v2 and 1.0 <= peak_r < 1.25:
            off = _v2_highband(peak_r)
        else:
            off = _granular(peak_r)
        tl = _tp_lock(frac_tp, rr)
        return max(off, tl) if tl is not None else off
    return offset


def _naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


# span massimo per request (1000 candele) per risoluzione
_SPAN_DAYS = {"MINUTE_5": 3, "MINUTE_15": 10, "MINUTE_30": 20}


def _fetch_range(cl, epic, frm, to):
    """Candele su [frm, to] paginando a finestre; prova 5m poi 15m poi 30m."""
    for res, span in _SPAN_DAYS.items():
        out, cur, ok = [], frm, True
        while cur < to:
            end = min(cur + timedelta(days=span), to)
            r = cl._session.get(
                cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                params={"resolution": res,
                        "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                        "to": end.strftime("%Y-%m-%dT%H:%M:%S"), "max": 1000},
                timeout=30)
            time.sleep(0.35)
            if r.status_code != 200:
                ok = False
                break
            out.extend(r.json().get("prices", []))
            cur = end
        if ok and out:
            seen, dedup = set(), []
            for c in out:
                k = c["snapshotTimeUTC"]
                if k not in seen:
                    seen.add(k)
                    dedup.append(c)
            return dedup, res
    return [], None


def _simulate(candles, direction, entry, r_dist, reward, rr, offset_fn):
    """Ritorna (profit_r, esito) con esito in {sl, tp, horizon}."""
    peak_r = 0.0
    peak_fav_move = 0.0
    for c in candles:
        frac_tp = (peak_fav_move / reward) if reward else None
        off = offset_fn(peak_r, frac_tp, rr)
        if direction == "short":
            sl_price = entry - off * r_dist
            tp_price = entry - reward
            adv = c["highPrice"]["ask"]; fav = c["lowPrice"]["ask"]
            if adv >= sl_price:
                return off, "sl"
            if fav <= tp_price:
                return rr, "tp"
            fav_move = entry - fav
        else:
            sl_price = entry + off * r_dist
            tp_price = entry + reward
            adv = c["lowPrice"]["bid"]; fav = c["highPrice"]["bid"]
            if adv <= sl_price:
                return off, "sl"
            if fav >= tp_price:
                return rr, "tp"
            fav_move = fav - entry
        if fav_move > peak_fav_move:
            peak_fav_move = fav_move
            peak_r = fav_move / r_dist
    last = candles[-1]
    if direction == "short":
        return (entry - last["closePrice"]["ask"]) / r_dist, "horizon"
    return (last["closePrice"]["bid"] - entry) / r_dist, "horizon"


def main() -> int:
    cfg = load_config()
    db = Database(cfg)
    cl = CapitalClient(cfg)
    cl.login()

    trades = (db._client.table("trades").select("*")
              .eq("status", "closed").not_.is_("pnl", "null")
              .order("id").execute().data)
    sample = [t for t in trades
              if (t.get("exit_reason") or "").startswith(PREFIXES)]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    rows, skipped = [], []
    for t in sample:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        sl_pct = sig.get("stop_loss") if sig else None
        tp_pct = sig.get("take_profit") if sig else None
        epic = (sig.get("epic") if sig else None) or EPIC_FALLBACK.get(t["asset"])
        if not (sl_pct and tp_pct and epic and t.get("closed_at")):
            skipped.append((t["id"], "manca sl/tp/epic/closed"))
            continue
        entry = float(t["entry_price"]); size = float(t["size"])
        r_dist = entry * float(sl_pct) / 100.0
        reward = entry * float(tp_pct) / 100.0
        rr = float(tp_pct) / float(sl_pct)
        opened = _naive(t["opened_at"]); closed = _naive(t["closed_at"])
        horizon = min(closed + timedelta(days=HORIZON_DAYS), now)
        candles, res = _fetch_range(cl, epic, opened, horizon)
        sel = [c for c in candles
               if opened <= datetime.fromisoformat(c["snapshotTimeUTC"]) <= horizon]
        if len(sel) < 3:
            skipped.append((t["id"], f"candele insufficienti ({len(sel)}, res={res})"))
            continue
        v1 = opened >= V1_LIVE
        v2 = opened >= V2_LIVE
        cf_r, cf_exit = _simulate(sel, t["direction"], entry, r_dist, reward, rr,
                                  make_offset_fn(v1, v2))
        # exit_R reale da close_price; fallback pnl (caveat valuta)
        if t.get("close_price") is not None:
            cp = float(t["close_price"])
            real_r = ((entry - cp) if t["direction"] == "short" else (cp - entry)) / r_dist
            real_src = "price"
        else:
            real_r = float(t["pnl"]) / (r_dist * size)
            real_src = "pnl"
        group = ("auto" if t["exit_reason"].startswith(PREFIXES[1]) else "proposta")
        rows.append({
            "id": t["id"], "asset": t["asset"], "dir": t["direction"],
            "group": group, "opened": t["opened_at"][:16], "res": res,
            "rule": "D" + ("+V1" if v1 else "") + ("+V2" if v2 else ""),
            "real_r": round(real_r, 3), "real_src": real_src,
            "cf_r": round(cf_r, 3), "cf_exit": cf_exit,
            "delta_r": round(cf_r - real_r, 3),
            "pnl_real": float(t["pnl"]),
            "delta_eur_approx": round((cf_r - real_r) * r_dist * size, 2),
        })
        print(f"  #{t['id']:>3} {t['asset']:<12} {t['direction']:<5} {group:<8} "
              f"real {real_r:+6.2f}R cf {cf_r:+6.2f}R ({cf_exit:<7}) "
              f"delta {cf_r - real_r:+6.2f}R [{res}]", flush=True)

    print(f"\n=== Copertura ===")
    print(f"trade chiusi dal monitor: {len(sample)} | simulati: {len(rows)} | scartati: {len(skipped)}")
    for sid, why in skipped:
        print(f"  scartato #{sid}: {why}")
    horizon_open = [r for r in rows if r["cf_exit"] == "horizon"]
    print(f"casi mark-to-market all'orizzonte (+{HORIZON_DAYS}gg): {len(horizon_open)}")

    if len(rows) < MIN_SAMPLE:
        print(f"\nCAMPIONE INSUFFICIENTE ({len(rows)} < {MIN_SAMPLE}): gate NON emesso (pre-registrato).")
        return 1

    def stats(subset, label):
        deltas = [r["delta_r"] for r in subset]
        mean = statistics.mean(deltas)
        med = statistics.median(deltas)
        print(f"{label:<28} n={len(subset):>3} delta_r medio {mean:+.3f} "
              f"mediana {med:+.3f} | real {sum(r['real_r'] for r in subset):+7.2f}R "
              f"cf {sum(r['cf_r'] for r in subset):+7.2f}R")
        return mean, med

    print(f"\n=== Gate pre-registrato (delta_r = controfattuale - reale) ===")
    mean_all, med_all = stats(rows, "SAMPLE COMPLETO")
    srt = sorted(rows, key=lambda r: -r["delta_r"])
    mean_no_top2, _ = stats(srt[2:], "senza i 2 migliori cf")
    mean_no_bot2, _ = stats(srt[:-2], "senza i 2 peggiori cf")
    print()
    for g in ("proposta", "auto"):
        sub = [r for r in rows if r["group"] == g]
        if sub:
            stats(sub, f"sottogruppo {g}")
    for d in ("long", "short"):
        sub = [r for r in rows if r["dir"] == d]
        if sub:
            stats(sub, f"direzione {d}")

    if mean_all >= 0.15 and mean_no_top2 >= 0.10 and med_all >= 0:
        verdict = "MONITOR DANNOSO"
    elif mean_all <= -0.15 and mean_no_bot2 <= -0.10:
        verdict = "MONITOR UTILE"
    else:
        verdict = "NEUTRO"
    print(f"\n>>> VERDETTO: {verdict} <<<")

    out = "docs/sprint6-monitor-replay.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"dettaglio per-trade scritto in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
