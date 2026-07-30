"""Test TradeSpinner (spec §10, criteri di done verificabili offline).
Standalone (assert). Uso: PYTHONPATH=$PWD .venv/bin/python jobs/test_spinner.py
"""
from __future__ import annotations

import sys

from src.spinner_config import (mu_total, classify, G_MIN, F_MAX_POS,
                                F_MAX_ACCOUNT, F_BUDGET_POS, MAX_POSITIONS,
                                MAX_PER_CLASS, ENTRY_CONFIRM_SCANS)
from src.spinner_odds import (evaluate_side, executable_f, portfolio_size,
                              select_target)


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        check.failed += 1
check.failed = 0


def main() -> int:
    print("=== §2.1 mu_total per classe ===")
    check("equity_index = 0.02+0.045 = 0.065", abs(mu_total("equity_index") - 0.065) < 1e-9)
    check("fx = 0 (carry nel financing)", mu_total("fx") == 0.0)
    check("crypto = 0", mu_total("crypto") == 0.0)
    check("gold = 0.025", abs(mu_total("gold") - 0.025) < 1e-9)

    print("=== classify instrumentType ===")
    check("INDICES -> equity_index", classify({"instrumentType": "INDICES"}) == "equity_index")
    check("CURRENCIES -> fx", classify({"instrumentType": "CURRENCIES"}) == "fx")
    check("COMMODITIES+GOLD -> gold", classify({"instrumentType": "COMMODITIES", "epic": "GOLD"}) == "gold")
    check("COMMODITIES altro -> commodity", classify({"instrumentType": "COMMODITIES", "epic": "OIL"}) == "commodity")

    print("=== §10.3 caso 1: indice LONG, ticket troppo grande -> not_executable ===")
    # US500-like: mu 0.065, paga fin 0.0215, min_notional 68 > equity 60
    e1 = evaluate_side("long", mu_total("equity_index"), 0.0215, 0.15, spread_bps=4,
                       min_notional_eur=68, equity=60, holding_days=60,
                       f_max_pos=F_MAX_POS, g_min=G_MIN, asset_class="equity_index")
    check("net_adj > 0 ma not_executable (min_notional>equity)", e1.net_adj > 0 and e1.status == "not_executable")

    print("=== §10.3 caso 2: indice SHORT, carry ricevuto ma net negativo -> below_gmin ===")
    # short paga fin_short -0.008 (riceve), ma il premio contro pesa di piu'
    e2 = evaluate_side("short", mu_total("equity_index"), -0.008, 0.15, spread_bps=4,
                       min_notional_eur=30, equity=60, holding_days=60,
                       f_max_pos=F_MAX_POS, g_min=G_MIN, asset_class="equity_index")
    check("net negativo -> below_gmin, non eligible", e2.net_adj < 0 and e2.status == "below_gmin")

    print("=== §10.3 caso 3: FX con financing negativo (carry puro) -> eligible ===")
    # mu 0, riceve 3%, sigma bassa 0.06, ticket piccolo
    e3 = evaluate_side("long", mu_total("fx"), -0.03, 0.06, spread_bps=2,
                       min_notional_eur=30, equity=60, holding_days=60,
                       f_max_pos=F_MAX_POS, g_min=G_MIN, asset_class="fx")
    check("carry positivo, g_exec >= G_MIN -> eligible", e3.status == "eligible" and e3.g_exec >= G_MIN)
    check("f_exec <= F_MAX_POS", e3.f_exec is not None and e3.f_exec <= F_MAX_POS)

    print("=== §10.4 filtro eseguibilita': min_notional>F_MAX_POS*equity mai eligible ===")
    n, f = executable_f(5.0, min_notional_eur=100, equity=60, f_max_pos=1.0)
    check("min_notional 100 > equity 60 -> nessuna f eseguibile", n is None and f is None)
    # anche con g teorica altissima, resta not_executable
    e4 = evaluate_side("long", 0.20, -0.10, 0.05, spread_bps=1, min_notional_eur=100,
                       equity=60, holding_days=60, f_max_pos=1.0, g_min=G_MIN,
                       asset_class="fx")
    check("g teorica enorme ma ticket grande -> not_executable", e4.status == "not_executable")

    print("=== not_rated: sigma assente -> not_rated ===")
    e5 = evaluate_side("long", 0.0, -0.03, None, spread_bps=2, min_notional_eur=30,
                       equity=60, holding_days=60, f_max_pos=1.0, g_min=G_MIN, asset_class="fx")
    check("sigma None -> not_rated", e5.status == "not_rated")

    print("=== F_BUDGET_POS: taglia di portafoglio (constants_log 2026-07-30) ===")
    # SW20-like: f_unit 0.177, budget 0.5 -> 3 unita' f 0.53, g sopra G_MIN
    p1 = portfolio_size(0.177, F_BUDGET_POS, remaining=1.5, f_max_pos=1.0,
                        net_adj=0.0529, sigma=0.1296, g_min=G_MIN)
    check("granulare: ~budget 0.5 (3 unita')", p1 is not None and p1[0] == 3
          and abs(p1[1] - 0.531) < 0.01 and p1[2] >= G_MIN)
    # ticket lumpy (AUDCHF-like f_unit 0.69 > budget): 1 unita' se sta nei cap
    p2 = portfolio_size(0.695, F_BUDGET_POS, remaining=0.97, f_max_pos=1.0,
                        net_adj=0.0461, sigma=0.0581, g_min=G_MIN)
    check("lumpy sopra budget ma nei cap: 1 unita'", p2 is not None and p2[0] == 1)
    # lumpy che NON sta nel budget residuo: None
    p3 = portfolio_size(0.695, F_BUDGET_POS, remaining=0.30, f_max_pos=1.0,
                        net_adj=0.0461, sigma=0.0581, g_min=G_MIN)
    check("lumpy fuori dal residuo: None", p3 is None)
    # g sotto G_MIN alla taglia budget -> aumenta n dentro i cap
    p4 = portfolio_size(0.0668, F_BUDGET_POS, remaining=1.5, f_max_pos=1.0,
                        net_adj=0.0374, sigma=0.1422, g_min=G_MIN)
    check("n cresce fino a g>=G_MIN dentro i cap", p4 is not None and p4[2] >= G_MIN)

    print("=== select_target: budget per posizione + isteresi + guard ===")
    board = [  # replica del board 2026-07-29 (equity 100)
        {"epic": "SW20", "side": "long", "asset_class": "equity_index",
         "min_notional_eur": 17.69, "net_adj": 0.05294, "sigma_ann": 0.12963},
        {"epic": "NL25", "side": "long", "asset_class": "equity_index",
         "min_notional_eur": 12.47, "net_adj": 0.04714, "sigma_ann": 0.12685},
        {"epic": "AUDCHF", "side": "long", "asset_class": "fx",
         "min_notional_eur": 69.48, "net_adj": 0.04606, "sigma_ann": 0.05807},
        {"epic": "VOO", "side": "long", "asset_class": "equity_stock",
         "min_notional_eur": 6.68, "net_adj": 0.03743, "sigma_ann": 0.14223},
    ]
    kw = dict(max_positions=MAX_POSITIONS, max_per_class=MAX_PER_CLASS,
              f_max_account=F_MAX_ACCOUNT, f_max_pos=F_MAX_POS,
              f_budget=F_BUDGET_POS, g_min=G_MIN,
              entry_confirm_scans=ENTRY_CONFIRM_SCANS)
    # tutti confermati: indice + fx carry; VOO alla taglia residua (f~0.27) ha
    # g < G_MIN e resta GIUSTAMENTE fuori (la soglia vale alla taglia aperta)
    consec = {(b["epic"], b["side"]): 5 for b in board}
    ch, rj = select_target(board, set(), consec, 100.0, **kw)
    check("diversifica: indice + carry fx", [c["epic"] for c in ch] == ["SW20", "AUDCHF"])
    check("NL25 scartato per classe", any(r[0] == "NL25" and "MAX_PER_CLASS" in r[2] for r in rj))
    check("VOO scartato: g sotto soglia alla taglia residua",
          any(r[0] == "VOO" and r[2] == "G_MIN_TAGLIA" for r in rj))
    check("f_sum <= F_MAX_ACCOUNT", sum(c["f_chosen"] for c in ch) <= F_MAX_ACCOUNT + 1e-9)
    check("somma g batte la singola migliore",
          sum(c["g_chosen"] for c in ch) > 0.0403)
    # isteresi consecutiva: AUDCHF nuovo (0 scan precedenti) resta fuori
    consec2 = dict(consec); consec2[("AUDCHF", "long")] = 0
    ch2, rj2 = select_target(board, set(), consec2, 100.0, **kw)
    check("nuovo non confermato -> ISTERESI", any(r[0] == "AUDCHF" and "ISTERESI" in r[2] for r in rj2))
    # held salta l'isteresi e ha priorita'
    ch3, _ = select_target(board, {("AUDCHF", "long")}, consec2, 100.0, **kw)
    check("held salta isteresi (hold)", any(c["epic"] == "AUDCHF" and c["reason"] == "hold" for c in ch3))
    # mai due versi dello stesso epic
    board2 = board + [{"epic": "SW20", "side": "short", "asset_class": "equity_index",
                       "min_notional_eur": 17.69, "net_adj": 0.03, "sigma_ann": 0.12963}]
    consec3 = {(b["epic"], b["side"]): 5 for b in board2}
    _, rj4 = select_target(board2, set(), consec3, 100.0, **kw)
    check("secondo verso stesso epic -> EPIC_DOPPIO",
          any(r[0] == "SW20" and r[2] == "EPIC_DOPPIO" for r in rj4))

    print(f"\n{'TUTTI I TEST PASSANO' if check.failed == 0 else str(check.failed) + ' TEST FALLITI'}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
