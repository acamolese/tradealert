"""Analisi: il cap di leva NOZIONALE e' cio' che rende il motore monocolore?

Ipotesi da falsificare (pre-registrata prima di guardare i risultati):
  F_MAX_POS=1.0 e' un vincolo sul NOZIONALE, ma il rischio e' la VOLATILITA'.
  Un cap uniforme sul nozionale ammette a taglia piena solo gli asset ad alta
  sigma e alto premio dichiarato (azionario), e costringe sotto la taglia
  ottimale (f_opt) proprio gli asset a bassa sigma (FX carry, bond), che poi
  vengono scartati da G_MIN "perche' rendono poco". Se e' vero, sostituendo il
  cap nozionale con un cap in volatilita' (f*sigma <= VOL_BUDGET) a PARITA' di
  rischio sull'azionario, il set eligible smette di essere 100% equity long.

Predizione se l'ipotesi e' VERA:
  - la maggioranza degli strumenti con f_opt > 1 e' non-equity;
  - sotto cap-vol il set eligible cresce e cambia composizione di classe;
  - compaiono eligible con sigma bassa (FX/carry) oggi below_gmin.
Predizione se l'ipotesi e' FALSA:
  - gli scartati restano scartati (net_adj <= 0 o g < G_MIN anche a f_opt):
    il problema non e' il cap ma l'assenza di premio, e il motore e' corretto.

ESITO 2026-08-14: IPOTESI FALSIFICATA. Il cap in volatilita' aggiunge 2 soli
strumenti (BRKB, COST), entrambi equity_stock: il set eligible resta 100% equity
long a qualunque cap di leva. Il cap nozionale NON e' la causa della staticita'.

CAUSA VERA (blocchi 6 e 7, aggiunti dopo la falsificazione): l'imbuto muore molto
piu' a monte, nella tabella PREMIUM di src/spinner_config.py. Con mu=0 per
fx/commodity/crypto l'unico modo di entrare e' il carry, che deve battere lo
spread ammortizzato su HOLDING_DAYS_MIN=60 (moltiplicatore 6.08x: 5bp di spread
= 3.0%/anno di costo). Al 2026-08-13: crypto 129 con net>0 -> 2 sopravvivono
allo spread; fx 13 -> 3; commodity 4 -> 0. E allungando l'orizzonte fino a 730
giorni la composizione NON cambia mai (solo equity_index + equity_stock).
Conclusione: su CFD Capital l'unico premio dichiarabile e' quello azionario, e il
motore lo riflette correttamente. La staticita' non e' un difetto di calibrazione,
e' una proprieta' del venue + della tabella di costanti.

NON tocca la produzione: legge spinner.odds_board e ricalcola offline.
Uso: python -m jobs.vol_budget_analysis [giorni]
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from src.config import load_config
from src.db import Database
from src.spinner_config import F_MAX_POS, G_MIN, MAX_PER_CLASS, F_MAX_ACCOUNT
from src.spinner_odds import executable_f, g_of_f

# Cap in volatilita': una posizione non contribuisce piu' di VOL_BUDGET di vol
# annua. 0.20 e' la sigma tipica di un indice azionario, quindi l'azionario
# resta dov'e' oggi (f~1.0): il confronto NON aumenta il rischio dove il
# sistema gia' opera, redistribuisce solo dove oggi e' sotto-taglia.
VOL_BUDGET_POS = 0.20
F_HARD_CAP = 3.0          # nessun asset oltre 3x nozionale, anche a sigma minuscola
EQUITY = 100.0            # equity di lavoro demo (SPINNER_EQUITY_CAP)


def _fetch(sp, table: str, cols: str, limit: int = 100000) -> list[dict]:
    """PostgREST cappa a 1000 righe: pagina esplicitamente."""
    out: list[dict] = []
    step, off = 1000, 0
    while off < limit:
        r = sp.table(table).select(cols).range(off, off + step - 1).execute().data
        out += r
        if len(r) < step:
            break
        off += step
    return out


def _eval_regime(row: dict, f_cap: float) -> tuple[float | None, float | None]:
    """(f_exec, g_exec) sotto un cap di leva dato. None se non eseguibile."""
    net_adj = row.get("net_adj")
    sigma = row.get("sigma_ann")
    mn = row.get("min_notional_eur")
    if net_adj is None or sigma is None or mn is None:
        return None, None
    net_adj, sigma, mn = float(net_adj), float(sigma), float(mn)
    if net_adj <= 0 or sigma <= 0 or mn <= 0:
        return None, None
    f_opt = net_adj / (sigma * sigma)
    units, f_exec = executable_f(f_opt, mn, EQUITY, f_cap)
    if units is None or f_exec is None:
        return None, None
    return f_exec, g_of_f(f_exec, net_adj, sigma)


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 18
    db = Database(load_config())
    sp = db._client.schema("spinner")

    cols = ("scan_date,epic,side,asset_class,sigma_ann,net_adj,mu_total,"
            "fin_annual,spread_bps,min_notional_eur,f_opt,f_exec,g_exec,status")
    rows = _fetch(sp, "odds_board", cols)
    dates = sorted({r["scan_date"] for r in rows})[-days:]
    rows = [r for r in rows if r["scan_date"] in dates]
    print(f"Righe: {len(rows)} | date: {len(dates)} ({dates[0]} -> {dates[-1]})")
    print(f"Regime A (attuale): f <= F_MAX_POS={F_MAX_POS}")
    print(f"Regime B (cap vol): f <= min({VOL_BUDGET_POS}/sigma, {F_HARD_CAP})")
    print(f"G_MIN={G_MIN} invariato in entrambi\n")

    last = dates[-1]
    today = [r for r in rows if r["scan_date"] == last]

    # --- 1. chi e' cappato dal vincolo nozionale, per classe ---
    capped: Counter = Counter()
    tot_cls: Counter = Counter()
    for r in today:
        if r.get("f_opt") is None:
            continue
        tot_cls[r["asset_class"]] += 1
        if float(r["f_opt"]) > F_MAX_POS:
            capped[r["asset_class"]] += 1
    print(f"--- 1. Strumenti con f_opt > {F_MAX_POS} (sotto-taglia forzata), {last}")
    for cls in sorted(tot_cls, key=lambda c: -capped[c]):
        n, t = capped[cls], tot_cls[cls]
        print(f"  {cls:<14} {n:>4}/{t:<4} ({100*n/t:.0f}%)")

    # --- 2. eligible per regime, ultimo giorno ---
    def classify_day(day_rows: list[dict]) -> tuple[list[dict], list[dict]]:
        elig_a, elig_b = [], []
        for r in day_rows:
            sigma = r.get("sigma_ann")
            if sigma is None or float(sigma) <= 0:
                continue
            fa, ga = _eval_regime(r, F_MAX_POS)
            fcap = min(VOL_BUDGET_POS / float(sigma), F_HARD_CAP)
            fb, gb = _eval_regime(r, fcap)
            if ga is not None and ga >= G_MIN:
                elig_a.append({**r, "f": fa, "g": ga})
            if gb is not None and gb >= G_MIN:
                elig_b.append({**r, "f": fb, "g": gb})
        return elig_a, elig_b

    ea, eb = classify_day(today)
    print(f"\n--- 2. Set eligible {last}: A={len(ea)}  B={len(eb)}")
    print("  composizione per classe:")
    ca, cb = Counter(r["asset_class"] for r in ea), Counter(r["asset_class"] for r in eb)
    for cls in sorted(set(ca) | set(cb)):
        print(f"    {cls:<14} A={ca.get(cls,0):>3}  B={cb.get(cls,0):>3}")
    print("  composizione per verso:")
    sa, sb = Counter(r["side"] for r in ea), Counter(r["side"] for r in eb)
    for s in sorted(set(sa) | set(sb)):
        print(f"    {s:<14} A={sa.get(s,0):>3}  B={sb.get(s,0):>3}")

    # --- 3. i nuovi entranti: chi passa solo sotto B ---
    keys_a = {(r["epic"], r["side"]) for r in ea}
    new = sorted([r for r in eb if (r["epic"], r["side"]) not in keys_a],
                 key=lambda r: -r["g"])
    print(f"\n--- 3. Eligible SOLO sotto cap-vol (nuove opportunita'): {len(new)}")
    print(f"  {'epic':<10} {'side':<6} {'classe':<14} {'sigma':>6} {'f_A':>5} "
          f"{'f_B':>5} {'g_A':>7} {'g_B':>7} {'fin':>7}")
    for r in new[:15]:
        fa, ga = _eval_regime(r, F_MAX_POS)
        fin = float(r.get("fin_annual") or 0)
        print(f"  {r['epic']:<10} {r['side']:<6} {r['asset_class']:<14} "
              f"{float(r['sigma_ann']):>6.3f} {(fa or 0):>5.2f} {r['f']:>5.2f} "
              f"{(ga or 0)*100:>6.2f}% {r['g']*100:>6.2f}% {fin*100:>6.2f}%")

    # --- 4. stabilita' nel tempo: quante volte cambia il TOP-3 ---
    print(f"\n--- 4. Turnover del ranking su {len(dates)} giorni (top-3 per g)")
    for label, cap_fn in (("A nozionale", lambda s: F_MAX_POS),
                          ("B cap-vol  ", lambda s: min(VOL_BUDGET_POS / s, F_HARD_CAP))):
        tops: list[tuple] = []
        for d in dates:
            day = [r for r in rows if r["scan_date"] == d]
            scored = []
            for r in day:
                sigma = r.get("sigma_ann")
                if sigma is None or float(sigma) <= 0:
                    continue
                f, g = _eval_regime(r, cap_fn(float(sigma)))
                if g is not None and g >= G_MIN:
                    scored.append((g, r["epic"], r["side"]))
            scored.sort(reverse=True)
            tops.append(tuple((e, s) for _, e, s in scored[:3]))
        changes = sum(1 for i in range(1, len(tops)) if tops[i] != tops[i - 1])
        distinct = len({t for t in tops})
        print(f"  {label}: {changes} cambi su {len(tops)-1} transizioni, "
              f"{distinct} top-3 distinti")

    # --- 5. il vincolo MAX_PER_CLASS diversifica davvero? ---
    print(f"\n--- 5. MAX_PER_CLASS={MAX_PER_CLASS}: classi presenti nel set eligible")
    per_day = defaultdict(set)
    for r in rows:
        sigma = r.get("sigma_ann")
        if sigma is None or float(sigma) <= 0:
            continue
        _, g = _eval_regime(r, F_MAX_POS)
        if g is not None and g >= G_MIN:
            per_day[r["scan_date"]].add(r["asset_class"])
    print(f"  regime A: classi distinte per giorno = "
          f"{sorted({len(v) for v in per_day.values()})} "
          f"(con MAX_PER_CLASS={MAX_PER_CLASS} e F_MAX_ACCOUNT={F_MAX_ACCOUNT}, "
          f"il numero di posizioni possibili e' <= classi distinte)")
    esempio = per_day[last]
    print(f"  classi {last}: {sorted(esempio)}")

    # --- 6. dove muore l'opportunita': manca il premio o lo mangia lo spread? ---
    print(f"\n--- 6. Imbuto per classe, {last} (miglior verso per strumento)")
    print(f"  {'classe':<14} {'strum':>6} {'net>0':>7} {'netadj>0':>9} "
          f"{'elig':>6}   morti per spread")
    by_cls: dict[str, dict] = defaultdict(
        lambda: {"n": set(), "net": set(), "adj": set(), "el": set()})
    for r in today:
        cls, epic = r["asset_class"], r["epic"]
        d = by_cls[cls]
        d["n"].add(epic)
        mu = float(r.get("mu_total") or 0)
        fin = float(r.get("fin_annual") or 0)
        net = (mu - fin) if r["side"] == "long" else (-mu - fin)
        if net > 0:
            d["net"].add(epic)
        if float(r.get("net_adj") or 0) > 0:
            d["adj"].add(epic)
        if r["status"] == "eligible":
            d["el"].add(epic)
    for cls in sorted(by_cls, key=lambda c: -len(by_cls[c]["n"])):
        d = by_cls[cls]
        morti = len(d["net"]) - len(d["adj"])
        print(f"  {cls:<14} {len(d['n']):>6} {len(d['net']):>7} {len(d['adj']):>9} "
              f"{len(d['el']):>6}   {morti}")

    # --- 7. sensibilita' all'orizzonte: lo spread e' ammortizzato su 60 giorni ---
    from src.spinner_config import HOLDING_DAYS_MIN
    from src.spinner_odds import spread_ann
    print(f"\n--- 7. Eligible al variare di HOLDING_DAYS (oggi {HOLDING_DAYS_MIN})")
    print(f"  {'giorni':>7} {'moltipl.':>9} {'eligible':>9}   composizione")
    for hd in (60, 120, 180, 365, 730):
        elig_cls: Counter = Counter()
        for r in today:
            sigma = r.get("sigma_ann")
            mn = r.get("min_notional_eur")
            if sigma is None or float(sigma) <= 0 or not mn:
                continue
            mu = float(r.get("mu_total") or 0)
            fin = float(r.get("fin_annual") or 0)
            net = (mu - fin) if r["side"] == "long" else (-mu - fin)
            net_adj = net - spread_ann(float(r.get("spread_bps") or 0), hd)
            if net_adj <= 0:
                continue
            sigma = float(sigma)
            f_opt = net_adj / (sigma * sigma)
            units, f_exec = executable_f(f_opt, float(mn), EQUITY, F_MAX_POS)
            if units is None:
                continue
            if g_of_f(f_exec, net_adj, sigma) >= G_MIN:
                elig_cls[r["asset_class"]] += 1
        tot = sum(elig_cls.values())
        comp = " ".join(f"{k}={v}" for k, v in sorted(elig_cls.items()))
        print(f"  {hd:>7} {365/hd:>9.2f} {tot:>9}   {comp}")


if __name__ == "__main__":
    main()
