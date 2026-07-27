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
            })
        except Exception:
            continue
        time.sleep(DETAIL_SLEEP)
    return anag


def load_universe(capital, rebuild=False):
    if not rebuild and CACHE.exists():
        age = (time.time() - CACHE.stat().st_mtime) / 86400
        if age < CACHE_MAX_AGE_DAYS:
            return json.loads(CACHE.read_text())
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

    equity = float(((capital.get_account_info().get("accounts") or [{}])[0]
                    .get("balance") or {}).get("balance") or 0.0)
    anag = load_universe(capital, rebuild=rebuild)
    today = datetime.now(timezone.utc).date().isoformat()
    log.info("equity=%.2f | universo=%d | scan_date=%s", equity, len(anag), today)

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
            of = instr.get("overnightFee") or {}
            fin_long = float(of.get("longRate") or 0.0)
            fin_short = float(of.get("shortRate") or 0.0)
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

    _build_target(sp, db, today, scfg)
    log.info("SCAN completo: scansionati %d | eseguibili %d | eligible %d",
             scanned, executable, eligible)
    print(f"universo {scanned} | eseguibili {executable} | eligible {eligible}")
    return 0


def _build_target(sp, db, today, scfg):
    """§4: ordina eligible per g_exec, max MAX_POSITIONS, max MAX_PER_CLASS,
    Sigma f_exec <= F_MAX_ACCOUNT, isteresi ENTRY_CONFIRM_SCANS in ingresso.
    Uscite immediate se g < G_EXIT (qui: non piu' eligible). Scrive target_portfolio."""
    elig = (sp.table("odds_board").select("*")
            .eq("scan_date", today).eq("status", "eligible")
            .order("g_exec", desc=True).execute().data)
    # conteggio scan consecutivi eligible per (epic,side) sugli ultimi giorni
    recent = (sp.table("odds_board").select("scan_date,epic,side,status")
              .neq("scan_date", today).eq("status", "eligible")
              .order("scan_date", desc=True).limit(400).execute().data)
    consec = {}
    for r in recent:
        consec[(r["epic"], r["side"])] = consec.get((r["epic"], r["side"]), 0) + 1

    chosen, per_class, f_sum = [], {}, 0.0
    for e in elig:
        if len(chosen) >= scfg.max_positions:
            break
        cls = e["asset_class"]
        if per_class.get(cls, 0) >= scfg.max_per_class:
            continue
        f = float(e["f_exec"] or 0)
        if f_sum + f > scfg.f_max_account:
            continue
        # isteresi: entra solo dopo ENTRY_CONFIRM_SCANS scansioni consecutive
        confirms = consec.get((e["epic"], e["side"]), 0) + 1  # +oggi
        if confirms < scfg.entry_confirm_scans:
            continue
        chosen.append(e)
        per_class[cls] = per_class.get(cls, 0) + 1
        f_sum += f

    open_now = {(r["epic"], r["side"]) for r in
                (sp.table("target_portfolio").select("epic,side").execute().data or [])}
    target = []
    for e in chosen:
        key = (e["epic"], e["side"])
        target.append({
            "as_of_date": today, "epic": e["epic"], "side": e["side"],
            "units": None, "f_exec": e["f_exec"], "g_exec": e["g_exec"],
            "reason": "hold" if key in open_now else "enter",
        })
    # units = f_exec * equity / min_notional (multiplo di minDealSize); qui lasciato
    # all'esecutore che valida col prezzo corrente (§6.3 tolleranza 5%)
    try:
        if target:
            sp.table("target_portfolio").upsert(target, on_conflict="as_of_date,epic").execute()
    except Exception:
        log.exception("upsert target_portfolio fallito")


if __name__ == "__main__":
    sys.exit(main())
