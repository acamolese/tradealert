"""Configurazione del controller di esposizione v2 (spec §7).

Modulo isolato dal Config v1 (src/config.py): v2 gira dietro flag, il rollback (§12)
non tocca v1. I parametri marcati NON CALIBRABILI dalla spec sono costanti di modulo,
non variabili d'ambiente: la liberta' di calibrarli e' il meccanismo che ha prodotto
il falso positivo precedente (§3, §13), quindi va rimossa a livello di codice.

MAX_BLOCKS non e' qui: e' calcolato a runtime da equity/leva reale (§2.2).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() == "true"


# --- NON CALIBRABILI (costanti di modulo, mai da env, mai in una griglia) ---
EWMA_LAMBDA: float = 0.94          # RiskMetrics, §3: fisso per convenzione
WARMUP_BARS: int = 250            # barre giornaliere prima di emettere un target (§3)
TRADING_DAYS: int = 252           # annualizzazione della sigma


@dataclass(frozen=True)
class ExposureConfig:
    enabled: bool                 # V2_EXPOSURE_ENABLED, master flag
    auto_execute: bool            # V2_AUTO_EXECUTE: se false calcola+propone ma NON
                                  # apre/chiude (in attesa del veto pieno §4.3)
    epic: str                     # strumento singolo (§1.1)
    block_margin_eur: float       # margine per blocco (§1)
    gap_tolerance: float          # g in N_max (§2.2)
    kill_equity_floor_eur: float  # circuit breaker (§2.3)
    sigma_target: float           # volatilita' target annualizzata (§4)
    max_scale: float              # cap sullo scaling (§4)
    hysteresis_days: int          # banda morta (§4.1)
    catastrophe_stop_pct: float   # stop di catastrofe, frazione del nozionale (§7.1)
    macro_scale_enabled: bool     # §5.1, default false: si logga ma non si applica
    use_guaranteed_stop: bool     # §7.2, da valutare

    # --- selezione dinamica dello strumento (2026-08-14) ---
    # Default OFF: il deploy non cambia comportamento finche' non si attiva.
    switch_enabled: bool          # V2_SWITCH_ENABLED
    switch_min_edge: float        # miglioramento RELATIVO di net_adj/sigma richiesto
    switch_stable_days: int       # giorni consecutivi in testa prima di muovere
    board_max_age_days: int       # oltre questa eta' il tabellone non e' affidabile

    # costanti riesposte per comodita' (restano non-calibrabili)
    ewma_lambda: float = EWMA_LAMBDA
    warmup_bars: int = WARMUP_BARS
    trading_days: int = TRADING_DAYS


def load_exposure_config() -> ExposureConfig:
    return ExposureConfig(
        enabled=_b("V2_EXPOSURE_ENABLED", False),
        auto_execute=_b("V2_AUTO_EXECUTE", False),
        epic=os.environ.get("V2_EPIC", "US500"),
        block_margin_eur=_f("BLOCK_MARGIN_EUR", 20.0),
        gap_tolerance=_f("GAP_TOLERANCE", 0.10),
        kill_equity_floor_eur=_f("KILL_EQUITY_FLOOR_EUR", 40.0),
        sigma_target=_f("SIGMA_TARGET", 0.15),
        max_scale=_f("MAX_SCALE", 2.0),
        hysteresis_days=int(_f("HYSTERESIS_DAYS", 2)),
        catastrophe_stop_pct=_f("CATASTROPHE_STOP_PCT", 0.075),
        macro_scale_enabled=_b("MACRO_SCALE_ENABLED", False),
        use_guaranteed_stop=_b("USE_GUARANTEED_STOP", False),
        switch_enabled=_b("V2_SWITCH_ENABLED", False),
        switch_min_edge=_f("V2_SWITCH_MIN_EDGE", 0.15),
        switch_stable_days=int(_f("V2_SWITCH_STABLE_DAYS", 2)),
        board_max_age_days=int(_f("V2_BOARD_MAX_AGE_DAYS", 2)),
    )
