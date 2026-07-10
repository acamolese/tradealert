"""Replay controfattuale Fase A: stream LLM vs stream shadow (Sprint 7).

Protocollo pre-registrato in docs/sprint7-fase-a-entry-shadow.md (sezione
"Replay controfattuale"). Confronto PAIRED: solo gli scan in cui esistono
sia un signal LLM sia un pick shadow entro la stessa finestra (±20 min),
cosi' i due selettori sono giudicati sullo stesso momento di mercato.

Per entrambi gli stream, stessa simulazione gia' validata (M3):
entry = open della prima candela HOUR successiva allo scan (ask per long,
bid per short), bracket TP + trailing live (make_offset_fn D+V1+V2),
SL-first conservativo, orizzonte 15 giorni. Riusa simulate() di
jobs/mae_analysis.py. Il pick shadow porta stop/target propri (1.5xATR,
rr 2); il signal LLM i suoi stop_loss/take_profit.

NON lanciare il verdetto aggregato prima della chiusura della Fase A
(avviso agenda): fino ad allora usare --only per singoli casi.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/entry_shadow_replay.py [--only PREFIX]
     --only 2026-07-07 : replay dei soli pair il cui scan inizia cosi'
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.db import Database

from jobs.mae_analysis import simulate
from jobs.monitor_close_replay import EPIC_FALLBACK
from jobs.validate_sim import load_candles

SHADOW_START = "2026-07-02T22:00:00+00:00"
MATCH_WINDOW_MIN = 20
HORIZON_DAYS = 15
MIN_PAIRS = 15          # sotto questa n il confronto e' solo descrittivo
GATE_DELTA = 0.15       # |delta expectancy| minimo per dichiarare un vincitore
GATE_DROP2 = 0.10       # delta residuo senza i 2 pair piu' favorevoli al vincitore


def _naive(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)


def replay_pick(cache, epic, scan_ts, direction, stop_pct, target_pct):
    """(exit_r, entry, n_candele) simulando il setup dallo scan; None se dati assenti."""
    if epic not in cache:
        cache[epic] = load_candles(epic)
    candles = [c for c in cache[epic]
               if scan_ts <= datetime.fromisoformat(c["ts"])
               <= scan_ts + timedelta(days=HORIZON_DAYS)]
    if len(candles) < 3:
        return None
    first = candles[0]
    entry = first["open_ask"] if direction == "long" else first["open_bid"]
    r_dist = entry * stop_pct / 100.0
    tp_reward = entry * target_pct / 100.0
    rr = target_pct / stop_pct
    exit_r, _ = simulate(candles, direction, entry, r_dist, tp_reward, rr)
    return exit_r, entry, len(candles)


def main() -> int:
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    cfg = load_config()
    db = Database(cfg)
    sigs = (db._client.table("signals")
            .select("id,asset,epic,direction,stop_loss,take_profit,created_at")
            .gte("created_at", SHADOW_START).order("created_at").execute().data)
    shadows = (db._client.table("monitoring_events").select("created_at,details")
               .eq("event_type", "entry_shadow")
               .gte("created_at", SHADOW_START).execute().data)
    win = timedelta(minutes=MATCH_WINDOW_MIN)
    sh = [(_naive(s["created_at"]), (s.get("details") or {}).get("pick"))
          for s in shadows]

    cache: dict[str, list[dict]] = {}
    pairs, skipped = [], 0
    for sig in sigs:
        if only and not sig["created_at"].startswith(only):
            continue
        st = _naive(sig["created_at"])
        near = [(t, p) for t, p in sh if abs(t - st) <= win]
        if not near:
            continue
        scan_ts, pick = near[0]
        sig_epic = sig.get("epic") or EPIC_FALLBACK.get(sig["asset"])
        llm = (replay_pick(cache, sig_epic, st, sig["direction"],
                           float(sig["stop_loss"]), float(sig["take_profit"]))
               if sig_epic and sig.get("stop_loss") and sig.get("take_profit") else None)
        shd = None
        if pick:
            p_epic = EPIC_FALLBACK.get(pick["asset"])
            if p_epic:
                shd = replay_pick(cache, p_epic, scan_ts, pick["direction"],
                                  float(pick["stop_pct"]), float(pick["target_pct"]))
        if llm is None or shd is None:
            skipped += 1
            continue
        pairs.append({"ts": sig["created_at"][:16],
                      "llm": f"{sig['asset']} {sig['direction']}", "llm_r": llm[0],
                      "shd": f"{pick['asset']} {pick['direction']}", "shd_r": shd[0]})

    print(f"pair simulati: {len(pairs)} | scartati (dati mancanti): {skipped}\n")
    for p in pairs:
        print(f" {p['ts']}  LLM {p['llm']:<20}{p['llm_r']:+.2f}R | "
              f"shadow {p['shd']:<20}{p['shd_r']:+.2f}R")

    if only or len(pairs) < 2:
        return 0

    m_llm = statistics.mean(p["llm_r"] for p in pairs)
    m_shd = statistics.mean(p["shd_r"] for p in pairs)
    delta = m_shd - m_llm
    deltas = sorted((p["shd_r"] - p["llm_r"] for p in pairs),
                    reverse=(delta > 0))
    drop2 = statistics.mean(deltas[2:]) if len(deltas) > 2 else 0.0
    print(f"\nexpectancy per pick: LLM {m_llm:+.3f}R | shadow {m_shd:+.3f}R | "
          f"delta shadow-LLM {delta:+.3f}R (senza top2 {drop2:+.3f}R)")
    if len(pairs) < MIN_PAIRS:
        print(f"n < {MIN_PAIRS}: solo descrittivo, nessun verdetto.")
    elif abs(delta) >= GATE_DELTA and abs(drop2) >= GATE_DROP2 and delta * drop2 > 0:
        print(f"VERDETTO: apre meglio lo stream {'SHADOW' if delta > 0 else 'LLM'}.")
    else:
        print("VERDETTO: NON CONCLUSIVO (delta sotto gate o non robusto).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
