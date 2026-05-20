"""Replay diagnostico della pipeline scanner a un istante storico.

Sprint 2 - indagine sul bias long. Permette di:

  scan    individua episodi di ribasso (drawdown >= 3% in 1-3 giorni)
          sui 5 asset core negli ultimi N giorni e calcola, per ognuno,
          l'istante di "entrata short" ex-post. Scrive /tmp/short_moments.json.

  replay  per ciascun momento ricostruisce le feature tecniche COM'ERANO
          a quell'istante (slice di candele 4H, stessa logica di
          compute_features e _collect_features: ultime 60 candele 4H) e
          rigira la pipeline: pre-filtro -> LLM rank_setups -> guardrail.
          Restituisce cosa ha proposto il modello sull'asset di interesse.

Limiti noti della ricostruzione storica (dichiarati nel report):
  - news, critical_events, economic_calendar NON sono ricostruibili
    a posteriori: vengono passati vuoti. I guardrail macro diventano
    quindi no-op. Il bias long/short e' comunque diagnosticabile perche'
    dipende dalla lettura tecnica del modello.
  - daily_pct_change / daily_range_pct / pct_from_daily_high vengono
    ricostruiti dalle candele 4H del giorno di calendario dell'istante T,
    non dal percentageChange live di Capital (buona approssimazione).
  - margin_factor / min_size non servono alla diagnosi e sono omessi.

Uso (lato VM):
    .venv/bin/python -m jobs.replay_signal scan --days 60
    .venv/bin/python -m jobs.replay_signal replay
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from src.capital_client import CapitalClient
from src.config import load_config
from src.features import compute_features
from src.llm_analyzer import LLMAnalyzer
from src.macro_guard import apply_macro_guardrails
from src.scanner import _prefilter_candidates

CORE = [
    ("Gold", "GOLD", "metal"),
    ("Brent Oil", "OIL_BRENT", "energy"),
    ("US500", "US500", "index"),
    ("Nasdaq 100", "US100", "index"),
    ("Bitcoin", "BTCUSD", "crypto"),
]
MOMENTS_FILE = "/tmp/short_moments.json"


def _mid(p: dict) -> float:
    return (p["bid"] + p["ask"]) / 2


def _dt(c: dict) -> datetime:
    raw = c.get("snapshotTimeUTC") or c["snapshotTime"]
    raw = raw.replace("/", "-")
    if raw.endswith("Z"):
        raw = raw[:-1]
    return datetime.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# SCAN: individua episodi di ribasso
# --------------------------------------------------------------------------

def detect_episodes(day_candles: list[dict], min_drop_pct: float = 3.0) -> list[dict]:
    """Episodi in cui il prezzo cala >= min_drop_pct entro 1-3 giorni dal
    picco. Ritorna episodi non sovrapposti (cluster -> tiene il peggiore)."""
    days = sorted(day_candles, key=_dt)
    raw: list[dict] = []
    for i in range(len(days) - 1):
        peak = _mid(days[i]["closePrice"])
        window = days[i + 1 : i + 4]
        if not window:
            continue
        trough = min(_mid(c["lowPrice"]) for c in window)
        drop = (peak - trough) / peak * 100
        if drop >= min_drop_pct:
            raw.append({
                "peak_day": _dt(days[i]),
                "peak_price": peak,
                "trough": trough,
                "drop_pct": round(drop, 2),
            })
    # dedup cluster: episodi a meno di 3 giorni l'uno dall'altro -> tieni
    # quello col drop maggiore.
    raw.sort(key=lambda e: e["peak_day"])
    episodes: list[dict] = []
    for e in raw:
        if episodes and (e["peak_day"] - episodes[-1]["peak_day"]).days < 3:
            if e["drop_pct"] > episodes[-1]["drop_pct"]:
                episodes[-1] = e
            continue
        episodes.append(e)
    return episodes


def entry_instant(candles_4h: list[dict], peak_day: datetime, peak_price: float,
                  trigger_pct: float = 1.2) -> datetime | None:
    """Istante operativo di entrata short: prima candela 4H dopo il picco
    in cui il calo dal picco supera trigger_pct. Cosi' al momento T il
    momentum ribassista e' gia' visibile ma resta spazio per il TP."""
    after = sorted((c for c in candles_4h if _dt(c) > peak_day), key=_dt)
    for c in after:
        if (peak_price - _mid(c["closePrice"])) / peak_price * 100 >= trigger_pct:
            return _dt(c)
    # fallback: candela piu' bassa entro 3 giorni
    window = [c for c in after if (_dt(c) - peak_day).days <= 3]
    if window:
        return _dt(min(window, key=lambda c: _mid(c["lowPrice"])))
    return None


def cmd_scan(cap: CapitalClient, days: int) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    moments: list[dict] = []
    for name, epic, _cls in CORE:
        day_candles = cap.get_prices(epic, resolution="DAY", max_bars=days + 10)
        c4h = cap.get_prices(epic, resolution="HOUR_4", max_bars=420)
        day_candles = [c for c in day_candles if _dt(c).timestamp() >= cutoff]
        episodes = detect_episodes(day_candles)
        for e in episodes:
            T = entry_instant(c4h, e["peak_day"], e["peak_price"])
            if T is None:
                continue
            moments.append({
                "asset": name,
                "epic": epic,
                "peak_day": e["peak_day"].date().isoformat(),
                "T": T.isoformat(),
                "peak_price": round(e["peak_price"], 4),
                "trough": round(e["trough"], 4),
                "drop_pct": e["drop_pct"],
            })
    moments.sort(key=lambda m: m["T"])
    with open(MOMENTS_FILE, "w") as fh:
        json.dump(moments, fh, indent=2)
    print(f"Episodi di ribasso individuati: {len(moments)}  -> {MOMENTS_FILE}\n")
    for m in moments:
        print(f"  {m['T'][:16]}  {m['asset']:<12} drop {m['drop_pct']:>5.2f}%  "
              f"picco {m['peak_day']} @ {m['peak_price']} -> {m['trough']}")


# --------------------------------------------------------------------------
# REPLAY: ricostruzione feature + pipeline
# --------------------------------------------------------------------------

def reconstruct_features(name: str, cls: str, c4h_all: list[dict],
                         T: datetime) -> dict[str, Any] | None:
    """Feature come le avrebbe viste lo scanner all'istante T: ultime 60
    candele 4H con snapshotTimeUTC <= T (stesso max_bars del live)."""
    sliced = sorted((c for c in c4h_all if _dt(c) <= T), key=_dt)
    if len(sliced) < 20:
        return None
    window = sliced[-60:]
    feat = compute_features(name, window, snapshot=None)
    feat["asset_class"] = cls
    feat["epic"] = name
    feat["news"] = []
    feat["market_status"] = "TRADEABLE"
    # snapshot-derived ricostruiti dalle candele 4H del giorno di T
    day_bars = [c for c in window if _dt(c).date() == T.date()]
    if day_bars:
        day_bars.sort(key=_dt)
        op = _mid(day_bars[0]["openPrice"])
        last = _mid(day_bars[-1]["closePrice"])
        hi = max(_mid(c["highPrice"]) for c in day_bars)
        lo = min(_mid(c["lowPrice"]) for c in day_bars)
        if op:
            feat["daily_pct_change"] = round((last - op) / op * 100, 2)
        if hi:
            feat["daily_range_pct"] = round((hi - lo) / hi * 100, 2)
            feat["pct_from_daily_high"] = round((last - hi) / hi * 100, 2)
    return feat


def cmd_replay(cap: CapitalClient, llm: LLMAnalyzer) -> None:
    with open(MOMENTS_FILE) as fh:
        moments = json.load(fh)

    # candele 4H lunghe per ogni asset, una volta sola
    c4h_cache = {
        epic: cap.get_prices(epic, resolution="HOUR_4", max_bars=420)
        for _n, epic, _c in CORE
    }

    for m in moments:
        T = datetime.fromisoformat(m["T"])
        asset = m["asset"]
        print("=" * 78)
        print(f"MOMENTO  {asset}  T={m['T'][:16]}  "
              f"drop ex-post {m['drop_pct']}%  ({m['peak_price']} -> {m['trough']})")

        feats: dict[str, dict[str, Any]] = {}
        for name, epic, cls in CORE:
            f = reconstruct_features(name, cls, c4h_cache[epic], T)
            if f:
                feats[name] = f

        af = feats.get(asset)
        if af:
            print(f"  feature {asset}: last={af.get('last_price')} "
                  f"rsi={af.get('rsi_14')} slope={af.get('trend_slope_pct')} "
                  f"bb_width={af.get('bb_width_pct')} "
                  f"pct_from_high20={af.get('pct_from_high_20')} "
                  f"daily_pct={af.get('daily_pct_change')}")

        is_weekend = T.weekday() >= 5
        context = {
            "is_weekend": is_weekend,
            "weekday": T.strftime("%A"),
            "traditional_markets_open": sum(
                1 for n, f in feats.items() if f.get("asset_class") != "crypto"
            ),
            "tradeable_count": len(feats),
            "critical_events": [],
            "economic_calendar": [],
        }

        # pre-filtro deterministico (stessa funzione del live)
        filtered, counts = _prefilter_candidates(feats, is_weekend=is_weekend)
        passed = asset in filtered
        print(f"  pre-filtro: {asset} {'PASSA' if passed else 'ESCLUSO'}  "
              f"(filtrati: {sorted(filtered.keys())})")

        # LLM su tutto il set pre-filtrato (pipeline fedele)
        proposals = llm.rank_setups(filtered or feats, context=context)
        proposals, _logs = apply_macro_guardrails(proposals, [], [])
        print(f"  LLM rank_setups -> {len(proposals)} proposal:")
        for p in proposals:
            print(f"    - {p.asset:<12} {p.direction:<5} score={p.score:<4} "
                  f"SL%={p.suggested_stop_pct} TP%={p.suggested_target_pct}")
            print(f"      thesis: {p.thesis}")

        # lettura isolata sull'asset di interesse (supplemento diagnostico)
        solo = llm.rank_setups({asset: af} if af else {}, context=context)
        print(f"  lettura isolata su {asset}:")
        for p in solo:
            rr = (p.suggested_target_pct / p.suggested_stop_pct
                  if p.suggested_stop_pct else None)
            print(f"    {p.direction} score={p.score} "
                  f"RR={rr:.2f}" if rr else f"    {p.direction} score={p.score}")
            print(f"    thesis: {p.thesis}")
            print(f"    key_factors: {p.key_factors}")
            print(f"    risks: {p.risks}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay diagnostico scanner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--days", type=int, default=60)
    sub.add_parser("replay")
    args = parser.parse_args()

    config = load_config()
    cap = CapitalClient(config)
    cap.login()

    if args.cmd == "scan":
        cmd_scan(cap, args.days)
    else:
        cmd_replay(cap, LLMAnalyzer(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
