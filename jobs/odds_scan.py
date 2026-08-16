"""Scansione notturna dell'odds board (spec §3-4). EXECUTION_TARGET=none in Fase 1:
scanner + board, nessun ordine.

Preflight §12.5: full scan giornaliero troppo per il rate limit -> anagrafica in
cache (file, refresh settimanale) con minDealSize/classe/leva; run giornaliero usa
la cache per l'eseguibilita' e fetch financing/spread/sigma solo sugli eseguibili.

Fase 1: anagrafica su universo gestibile (nav marketnavigation con cap), equity dal
conto reale (filtro di eseguibilita' vero), catalogo dal conto sulla VM (market data
identico a demo). L'isolamento forte (utente/ruolo demo) precede la fase demo.

Uso: PYTHONPATH=$PWD .venv/bin/python -m jobs.odds_scan [--rebuild-universe]
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config
from src.spinner_config import (
    load_spinner_config, mu_total, classify, HOLDING_DAYS_MIN, F_MAX_POS,
    F_MAX_ACCOUNT, MAX_POSITIONS, MAX_PER_CLASS, ENTRY_CONFIRM_SCANS,
    SPREAD_SAMPLES_MIN,
)
from src.spinner_odds import evaluate_side
from src.volatility import ewma_sigma

log = logging.getLogger(__name__)

CACHE = Path(__file__).resolve().parent.parent / "data" / "spinner_universe.json"
CACHE_MAX_AGE_DAYS = 7
UNIVERSE_CAP = 700           # cap Fase 1 (dichiarato): allarga nel refresh
DETAIL_SLEEP = 0.12         # throttle richieste


def _mid(snap):
    b, o = snap.get("bid"), snap.get("offer")
    return ((float(b) + float(o)) / 2) if (b and o) else (float(b) if b else None)


def build_universe(capital):
    """Naviga marketnavigation e costruisce l'anagrafica (epic, classe, minDealSize,
    prezzo, leva, valuta, spread_bps). Throttled, con cap Fase 1."""
    seen, out = set(), []
    r = capital._session.get(capital._url("/marketnavigation"),
                             headers=capital._auth_headers(), timeout=20)
    top = r.json().get("nodes", []) if r.status_code == 200 else []
    queue = [n["id"] for n in top]
    while queue and len(out) < UNIVERSE_CAP:
        nid = queue.pop(0)
        try:
            r = capital._session.get(capital._url(f"/marketnavigation/{nid}"),
                                     headers=capital._auth_headers(), timeout=20)
            if r.status_code != 200:
                continue
            j = r.json()
            queue.extend(n["id"] for n in j.get("nodes", []))
            for m in j.get("markets", []):
                ep = m.get("epic")
                if not ep or ep in seen:
                    continue
                seen.add(ep)
                out.append(ep)
                if len(out) >= UNIVERSE_CAP:
                    break
        except Exception:
            continue
        time.sleep(0.05)
    # dettagli anagrafici (minDealSize, leva, classe) per ogni epic
    from src.executor import _market_meta
    from src.leverage import real_leverage
    lev = capital.get_leverages_map()
    anag = []
    for ep in out:
        try:
            mk = capital.get_market(ep)
            instr = mk.get("instrument", {}) or {}
            cls = classify({**instr, "epic": ep})
            if cls == "excluded":
                continue
            meta = _market_meta(mk, leverages_map=lev, use_real_leverage=True)
            anag.append({
                "epic": ep, "asset_class": cls, "min_size": meta["min_size"],
                "real_leverage": real_leverage(ep) or (1.0 / meta["margin_factor"] if meta["margin_factor"] else None),
                "currency": instr.get("currency"),
                # name e tipo servono a RIclassificare al load senza rifare 700
                # get_market: la classe in cache non e' autoritativa (vedi load_universe)
                "name": instr.get("name") or ep,
                "instrument_type": (instr.get("instrumentType") or instr.get("type") or ""),
            })
        except Exception:
            continue
        time.sleep(DETAIL_SLEEP)
    return anag


def load_universe(capital, rebuild=False):
    """Anagrafica dell'universo, con cache su file (700 get_market sono lenti).

    La CLASSE non e' mai letta dalla cache: viene ricalcolata a ogni load dal
    `name` memorizzato. Motivo (bug 2026-08-16): asset_class era congelata nel
    file, quindi il fix di classificazione del 14/08 sarebbe entrato in vigore
    solo alla scadenza della cache, 4 giorni dopo e in silenzio. Una cache di
    DATI non deve contenere il risultato di una DECISIONE che puo' cambiare.
    """
    if not rebuild and CACHE.exists():
        age = (time.time() - CACHE.stat().st_mtime) / 86400
        if age < CACHE_MAX_AGE_DAYS:
            anag = json.loads(CACHE.read_text())
            if all(a.get("name") for a in anag):
                riclass = 0
                for a in anag:
                    cls = classify({"epic": a["epic"], "name": a["name"],
                                    "type": a.get("instrument_type", "")})
                    if cls != a.get("asset_class"):
                        riclass += 1
                    a["asset_class"] = cls
                if riclass:
                    log.info("anagrafica: %d strumenti riclassificati rispetto alla cache",
                             riclass)
                return [a for a in anag if a["asset_class"] != "excluded"]
            log.info("cache senza 'name' (formato pre-2026-08-16): ricostruisco")
    log.info("(ri)costruisco anagrafica universo...")
    anag = build_universe(capital)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(anag))
    log.info("anagrafica: %d strumenti", len(anag))
    return anag


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rebuild = "--rebuild-universe" in sys.argv

    from src.capital_client import CapitalClient
    from src.db import Database
    from src.risk import quote_to_ref_factor

    v1 = load_config()
    scfg = load_spinner_config()
    db = Database(v1)
    sp = db._client.schema("spinner")
    capital = CapitalClient(v1)
    capital.login()

    equity_raw = float(((capital.get_account_info().get("accounts") or [{}])[0]
                        .get("balance") or {}).get("balance") or 0.0)
    # cap dell'equity di lavoro (demo: rispecchia il reale invece dei ~1000 EURd)
    equity = min(equity_raw, scfg.equity_cap) if scfg.equity_cap else equity_raw
    anag = load_universe(capital, rebuild=rebuild)
    today = datetime.now(timezone.utc).date().isoformat()
    log.info("equity=%.2f (raw %.2f, cap %s, env %s) | universo=%d | scan_date=%s",
             equity, equity_raw, scfg.equity_cap, v1.capital_env, len(anag), today)

    scanned = executable = eligible = 0
    rows = []
    fx_cache = {}
    for a in anag:
        scanned += 1
        ep, cls = a["epic"], a["asset_class"]
        try:
            mk = capital.get_market(ep)
            instr = mk.get("instrument", {}) or {}
            snap = mk.get("snapshot", {}) or {}
            price = _mid(snap)
            if not price:
                continue
            ccy = instr.get("currency")
            if ccy not in fx_cache:
                fx_cache[ccy] = quote_to_ref_factor(ccy, capital) or 1.0
            q2r = fx_cache[ccy]
            min_notional = a["min_size"] * price * q2r
            if equity <= 0 or min_notional / equity > F_MAX_POS:
                continue   # not_executable: non registrato (Fase 1, universo grande)
            executable += 1
            # dettagli freschi solo sugli eseguibili
            # Capital overnightFee: rate NEGATIVO = addebito (costo) al cliente. La
            # spec usa fin = tasso PAGATO (negativo = ricevuto), quindi fin = -rate.
            # Confermato dal primo scan (indici long non "ricevono" financing) e da
            # verificare empiricamente §12.8 (posizione demo aperta il 2026-07-27).
            of = instr.get("overnightFee") or {}
            fin_long = -float(of.get("longRate") or 0.0)
            fin_short = -float(of.get("shortRate") or 0.0)
            b, o = snap.get("bid"), snap.get("offer")
            spread_bps = ((float(o) - float(b)) / price * 10000.0) if (b and o) else None
            closes = []
            for p in capital.get_prices(ep, resolution="DAY", max_bars=300):
                cp = p.get("closePrice") or {}
                if cp.get("bid") and cp.get("ask"):
                    closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
            sigma = ewma_sigma(closes)
            mu = mu_total(cls)
            for side, fin in (("long", fin_long), ("short", fin_short)):
                ev = evaluate_side(side, mu, fin, sigma, spread_bps or 999,
                                   min_notional, equity, HOLDING_DAYS_MIN,
                                   F_MAX_POS, scfg.g_min, asset_class=cls)
                if ev.status == "eligible":
                    eligible += 1
                rows.append({
                    "scan_date": today, "epic": ep, "asset_class": cls, "side": side,
                    "price": round(price, 5), "min_notional_eur": round(min_notional, 2),
                    "real_leverage": a.get("real_leverage"),
                    "spread_bps": round(spread_bps, 2) if spread_bps else None,
                    "spread_samples": 1,
                    "fin_annual": fin, "sigma_ann": round(sigma, 5) if sigma else None,
                    "mu_total": mu, "net_drift": round(ev.net, 5),
                    "net_adj": round(ev.net_adj, 5),
                    "f_opt": round(ev.f_opt, 3) if ev.f_opt else None,
                    "f_exec": round(ev.f_exec, 3) if ev.f_exec else None,
                    "g_exec": round(ev.g_exec, 5) if ev.g_exec is not None else None,
                    "status": ev.status,
                })
        except Exception:
            log.exception("scan %s fallito", ep)
        time.sleep(DETAIL_SLEEP)

    # scrivi odds_board (upsert sul giorno)
    for chunk in (rows[i:i + 200] for i in range(0, len(rows), 200)):
        try:
            sp.table("odds_board").upsert(chunk, on_conflict="scan_date,epic,side").execute()
        except Exception:
            log.exception("upsert odds_board fallito")

    _build_target(sp, db, today, scfg, equity)
    log.info("SCAN completo: scansionati %d | eseguibili %d | eligible %d",
             scanned, executable, eligible)
    print(f"universo {scanned} | eseguibili {executable} | eligible {eligible}")
    return 0


def consecutive_eligible(sp, today, lookback_days=10):
    """(epic,side) -> n. di scan CONSECUTIVI (dal piu' recente prima di oggi)
    in cui era eligible. Un giorno di scan senza quella riga interrompe il conteggio
    (prima si contavano le occorrenze su tutto lo storico, non la consecutivita')."""
    from collections import defaultdict
    dates = sorted({r["scan_date"] for r in
                    (sp.table("odds_board").select("scan_date")
                     .neq("scan_date", today).order("scan_date", desc=True)
                     .limit(4000).execute().data or [])}, reverse=True)[:lookback_days]
    if not dates:
        return {}
    rows = (sp.table("odds_board").select("scan_date,epic,side")
            .eq("status", "eligible").in_("scan_date", dates)
            .limit(2000).execute().data or [])
    by_date = defaultdict(set)
    for r in rows:
        by_date[r["scan_date"]].add((r["epic"], r["side"]))
    consec = {}
    for key in {k for s in by_date.values() for k in s}:
        n = 0
        for d in dates:                       # dal piu' recente, si ferma al primo buco
            if key in by_date[d]:
                n += 1
            else:
                break
        consec[key] = n
    return consec


def _build_target(sp, db, today, scfg, equity):
    """§4 + F_BUDGET_POS (constants_log 2026-07-30): eligible per g_exec, budget di
    leva per posizione, isteresi consecutiva in ingresso (le posizioni gia' in
    target la saltano: continuita' buy&hold). Uscite: non piu' eligible.
    Riscrive le righe del giorno (delete+insert: un re-run non lascia residui)."""
    from src.spinner_config import F_BUDGET_POS
    from src.spinner_odds import select_target

    elig = (sp.table("odds_board").select("*")
            .eq("scan_date", today).eq("status", "eligible")
            .order("g_exec", desc=True).execute().data)
    consec = consecutive_eligible(sp, today)

    # posizioni nell'ULTIMO target (non tutta la storia): sono gli hold
    prev = (sp.table("target_portfolio").select("as_of_date,epic,side")
            .order("as_of_date", desc=True).limit(50).execute().data or [])
    held = ({(r["epic"], r["side"]) for r in prev if r["as_of_date"] == prev[0]["as_of_date"]}
            if prev else set())

    chosen, rejects = select_target(
        elig, held, consec, equity,
        max_positions=scfg.max_positions, max_per_class=scfg.max_per_class,
        f_max_account=scfg.f_max_account, f_max_pos=scfg.f_max_pos,
        f_budget=F_BUDGET_POS, g_min=scfg.g_min,
        entry_confirm_scans=scfg.entry_confirm_scans)
    for ep, side, why in rejects:
        log.info("target: scartato %s %s (%s)", ep, side, why)

    target = [{
        "as_of_date": today, "epic": c["epic"], "side": c["side"],
        # units = n. lotti minimi alla taglia di portafoglio; l'esecutore ricalcola
        # col prezzo fresco da f_exec (§6.3 tolleranza 5%).
        "units": c["units"], "f_exec": c["f_chosen"], "g_exec": c["g_chosen"],
        "reason": c["reason"],
    } for c in chosen]
    try:
        sp.table("target_portfolio").delete().eq("as_of_date", today).execute()
        if target:
            sp.table("target_portfolio").insert(target).execute()
    except Exception:
        log.exception("scrittura target_portfolio fallita")
    log.info("target %s: %s (f_sum %.2f)", today,
             [(c["epic"], c["side"], c["f_chosen"]) for c in chosen],
             sum(c["f_chosen"] for c in chosen))


if __name__ == "__main__":
    sys.exit(main())
