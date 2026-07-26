"""Test del core v2 (spec §11, criteri di done verificabili offline).
Standalone (assert), niente pytest richiesto, niente DB/API.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/test_exposure_v2.py
"""
from __future__ import annotations

import sys

from src.exposure_config import ExposureConfig, WARMUP_BARS
from src.exposure_controller import compute_n_max, plan_exposure
from src.volatility import ewma_sigma


CFG = ExposureConfig(
    enabled=True, auto_execute=False, epic="US500", block_margin_eur=20.0, gap_tolerance=0.10,
    kill_equity_floor_eur=40.0, sigma_target=0.15, max_scale=2.0, hysteresis_days=2,
    catastrophe_stop_pct=0.075, macro_scale_enabled=False, use_guaranteed_stop=False,
)


def sigma_for_blocks(n: int) -> float:
    """sigma che produce scale ~= n (blocks_raw = sigma_target/sigma)."""
    return CFG.sigma_target / n


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        check.failed += 1
check.failed = 0


def main() -> int:
    print("=== §2.2  N_max calcolato dalla formula ===")
    check("E=60 m=20 L=20 g=0.10 -> N_max=1", compute_n_max(60, 20, 20, 0.10) == 1)
    check("E=60 m=20 L=20 g=0.02 -> N_max=2", compute_n_max(60, 20, 20, 0.02) == 2)
    check("E=200 m=20 L=20 g=0.10 -> N_max=3", compute_n_max(200, 20, 20, 0.10) == 3)

    print("=== §3/§11.12  warm-up: sotto 250 barre -> 0 blocchi ===")
    check("ewma_sigma con <251 chiusure -> None", ewma_sigma([4000.0] * 100) is None)
    rising = [4000.0 * (1.0003 ** i) * (1 + 0.01 * ((-1) ** i)) for i in range(400)]
    check("ewma_sigma con 400 chiusure -> float positivo",
          isinstance(ewma_sigma(rising), float) and ewma_sigma(rising) > 0)
    p = plan_exposure(None, 0, [], 60, 20, CFG)
    check("sigma None (warm-up) -> target 0, hold", p.blocks_target == 0 and p.delta == 0)

    print("=== §11.3  isteresi: oscillazione 1->2->1 su 3 giorni = zero ordini ===")
    E = 200  # n_max=3, spazio per 2
    deltas = []
    # g1: tgt=1, current=1  |  g2: tgt=2, current=1, recent=[1]  |  g3: tgt=1, current=1
    deltas.append(plan_exposure(sigma_for_blocks(1), 1, [1, 1], E, 20, CFG).delta)
    deltas.append(plan_exposure(sigma_for_blocks(2), 1, [1], E, 20, CFG).delta)
    deltas.append(plan_exposure(sigma_for_blocks(1), 1, [1, 2], E, 20, CFG).delta)
    check(f"tre giorni oscillanti -> ordini={deltas} tutti 0", all(d == 0 for d in deltas))

    print("=== §11.4  riduzioni bypassano l'isteresi, aumenti no ===")
    red = plan_exposure(sigma_for_blocks(1), 2, [2, 2], E, 20, CFG)  # tgt 1 < current 2
    check("riduzione 2->1 immediata (delta -1, close)", red.delta == -1 and red.action == "close")
    up_wait = plan_exposure(sigma_for_blocks(2), 1, [1], E, 20, CFG)  # tgt 2, non stabile
    check("aumento 1->2 non stabile -> hold (delta 0)", up_wait.delta == 0 and up_wait.action == "hold")
    up_go = plan_exposure(sigma_for_blocks(2), 1, [2], E, 20, CFG)    # tgt 2 gia' ieri
    check("aumento 1->2 stabile 2gg -> eseguo (delta +1, open)", up_go.delta == 1 and up_go.action == "open")

    print("=== §11.5  nessun percorso apre short ===")
    ok_long = True
    for cur in range(0, 4):
        for s in (0.05, 0.15, 0.4, None):
            pl = plan_exposure(s, cur, [cur], E, 20, CFG)
            if pl.blocks_to_reach < 0 or pl.blocks_target < 0 or pl.action == "short":
                ok_long = False
    check("blocks_to_reach e target sempre >= 0, mai 'short'", ok_long)

    print("=== §2.3  circuit breaker: equity < floor -> HALT, chiudi tutto ===")
    halt = plan_exposure(sigma_for_blocks(1), 2, [1, 1], 30, 20, CFG)
    check("equity 30 < floor 40 -> halt, delta -2", halt.action == "halt" and halt.delta == -2)

    print("=== §4.1  N_max sceso sotto i correnti -> riduzione immediata ===")
    # equity bassa: n_max piccolo mentre correnti alti
    shrink = plan_exposure(sigma_for_blocks(1), 3, [3, 3], 60, 20, CFG)  # n_max=1
    check("n_max=1 < correnti 3 -> close a 1 (delta -2)",
          shrink.n_max == 1 and shrink.blocks_to_reach == 1 and shrink.delta == -2)

    print(f"\n{'TUTTI I TEST PASSANO' if check.failed == 0 else str(check.failed) + ' TEST FALLITI'}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
