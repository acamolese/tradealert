"""Test TradeSpinner (spec §10, criteri di done verificabili offline).
Standalone (assert). Uso: PYTHONPATH=$PWD .venv/bin/python jobs/test_spinner.py
"""
from __future__ import annotations

import sys

from src.spinner_config import mu_total, classify, G_MIN, F_MAX_POS
from src.spinner_odds import evaluate_side, executable_f


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

    print(f"\n{'TUTTI I TEST PASSANO' if check.failed == 0 else str(check.failed) + ' TEST FALLITI'}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
