"""Caccia al CARRY: strumenti che PAGANO a tenerli, non che costano.

Nasce dal fix financing del 2026-08-17: il fattore 3.65 mancante amplificava di
altrettanto anche gli INCASSI, non solo i costi. Il tabellone mostrava "ZARJPY
riceve 4.3%" e lo scartava perche' sotto G_MIN: quel 4.3% e' in realta' 15.7%.

Idea: su CFD il prezzo non ha premio dichiarato per fx/commodity/crypto (mu=0),
quindi il rendimento atteso di quelle posizioni E' il carry. Dove il carry e'
incassato e supera i costi di giro, esiste una posizione a deriva attesa positiva
che non dipende da nessuna previsione di prezzo. E' l'opposto del long azionario a
leva, che paga 7.84% per inseguire un premio del 6.5%.

Il rischio non e' nascosto: il carry trade guadagna a gocce e perde a strappi
(svalutazioni). Per questo qui si misura anche sigma e il rapporto carry/sigma, e
si scarta chi non regge un movimento avverso plausibile.

Sola lettura: nessun ordine.
Uso: python -m jobs.carry_hunt [top_n]
"""
from __future__ import annotations

import sys
import time
from collections import Counter

from src.config import load_config
from src.capital_client import CapitalClient
from src.db import Database
from src.spinner_config import classify, mu_total, HOLDING_DAYS_MIN
from src.spinner_odds import annual_financing, spread_ann, g_of_f
from src.volatility import ewma_sigma
from src.risk import quote_to_ref_factor
from jobs.odds_scan import load_universe, _mid

EQUITY_DEMO = 100.0
EQUITY_REAL = 58.0


def main() -> int:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    v1 = load_config()
    capital = CapitalClient(v1)
    capital.login()
    anag = load_universe(capital)
    print(f"universo: {len(anag)} strumenti | {Counter(a['asset_class'] for a in anag)}\n")

    fx_cache: dict[str, float] = {}
    rows = []
    for a in anag:
        ep, cls = a["epic"], a["asset_class"]
        try:
            mk = capital.get_market(ep)
            instr = mk.get("instrument", {}) or {}
            snap = mk.get("snapshot", {}) or {}
            price = _mid(snap)
            if not price:
                continue
            of = instr.get("overnightFee") or {}
            iv = of.get("swapChargeInterval")
            fin_long = annual_financing(of.get("longRate"), iv)
            fin_short = annual_financing(of.get("shortRate"), iv)
            # interessa solo chi INCASSA (fin negativo = ricevuto) su almeno un verso
            if min(fin_long, fin_short) >= 0:
                continue
            ccy = instr.get("currency")
            if ccy not in fx_cache:
                fx_cache[ccy] = quote_to_ref_factor(ccy, capital) or 1.0
            q2r = fx_cache[ccy]
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
                if fin >= 0:
                    continue
                net = (mu - fin) if side == "long" else (-mu - fin)
                net_adj = net - spread_ann(spread_bps or 0, HOLDING_DAYS_MIN)
                rows.append({
                    "epic": ep, "name": a.get("name", ep)[:26], "cls": cls, "side": side,
                    "carry": -fin, "mu": mu, "net": net, "net_adj": net_adj,
                    "sigma": sigma, "spread_bps": spread_bps or 0,
                    "min_notional": a["min_size"] * price * q2r,
                })
        except Exception:
            continue
        time.sleep(0.1)

    rows.sort(key=lambda r: -r["net_adj"])
    print(f"STRUMENTI CHE PAGANO UN CARRY: {len(rows)} versi\n")
    print(f"{'epic':<9} {'side':<6} {'classe':<10} {'carry':>7} {'net_adj':>8} "
          f"{'sigma':>6} {'c/s':>5} {'spr':>6} {'min€':>7}  nome")
    for r in rows[:top_n]:
        cs = (r["net_adj"] / r["sigma"]) if r["sigma"] else 0
        sg = f"{r['sigma']:.3f}" if r["sigma"] else "  n/d"
        print(f"{r['epic']:<9} {r['side']:<6} {r['cls']:<10} {r['carry']*100:>6.2f}% "
              f"{r['net_adj']*100:>7.2f}% {sg:>6} {cs:>5.2f} {r['spread_bps']:>5.0f}bp "
              f"{r['min_notional']:>7.2f}  {r['name']}")

    # cosa sarebbe eseguibile davvero sui due conti
    print("\nESEGUIBILITA' (min notional vs equity):")
    for label, eq in (("REALE 58€", EQUITY_REAL), ("DEMO 100€", EQUITY_DEMO)):
        ok = [r for r in rows if r["net_adj"] > 0 and r["min_notional"] <= eq]
        print(f"  {label}: {len(ok)} versi con net_adj>0 e 1 lotto entro l'equity")
        for r in ok[:6]:
            f_unit = r["min_notional"] / eq
            g = g_of_f(f_unit, r["net_adj"], r["sigma"]) if r["sigma"] else 0
            print(f"    {r['epic']:<9} {r['side']:<6} f={f_unit:.2f} "
                  f"net_adj={r['net_adj']*100:.2f}% g@1lotto={g*100:+.2f}%/anno "
                  f"({g*eq:+.2f}€/anno)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
