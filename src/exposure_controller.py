"""Controller di esposizione v2 (spec §2.2, §4). Logica PURA e deterministica:
niente I/O (DB, Capital, Telegram). Il job la orchestra; qui si calcola e si testa.

Il sistema non seleziona "il setup migliore": calcola quanti blocchi vuole essere
esposto e apre/chiude blocchi per raggiungere quel numero. Un blocco = un quanto di
esposizione (BLOCK_MARGIN_EUR di margine). Solo long. Un solo strumento.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.exposure_config import ExposureConfig


def compute_n_max(equity_eur: float, block_margin_eur: float,
                  real_leverage: float, gap_tolerance: float) -> int:
    """Numero massimo di blocchi da sopravvivenza al gap (§2.2):

        N_max = floor( E / ( m * (1 + L*g) ) )

    Derivazione: dopo un gap avverso di frazione g sul nozionale, l'equity residua
    deve restare sopra il margine richiesto:  E - N*m*L*g > N*m.
    NON e' un parametro: e' calcolato da equity e leva reale (lette da API).
    """
    denom = block_margin_eur * (1.0 + real_leverage * gap_tolerance)
    if denom <= 0:
        return 0
    return max(0, int(math.floor(equity_eur / denom)))


def compute_scale(sigma_hat: float, cfg: ExposureConfig,
                  macro_scale: float = 1.0) -> tuple[float, float]:
    """Ritorna (scale_raw, scale_applied). Volatility targeting (§4):
        scale_raw = SIGMA_TARGET / sigma
        scale     = clip(scale_raw, 0, MAX_SCALE) * macro_scale
    macro_scale e' 1.0 quando MACRO_SCALE_ENABLED e' off (§5.1): in v1 si logga il
    valore macro ma non entra nel calcolo (il job passa 1.0 se il flag e' off).
    """
    if sigma_hat is None or sigma_hat <= 0:
        return 0.0, 0.0
    scale_raw = cfg.sigma_target / sigma_hat
    scale = min(max(scale_raw, 0.0), cfg.max_scale)
    return scale_raw, scale * macro_scale


def compute_target_blocks(scale_applied: float, n_max: int) -> tuple[float, int]:
    """Ritorna (blocks_raw, blocks_target). Con BASE_NOTIONAL = BLOCK_MARGIN*L,
    blocks_raw coincide con lo scale (1 blocco a volatilita' pari al target, §4).
    """
    blocks_raw = scale_applied
    blocks_tgt = int(min(max(round(blocks_raw), 0), max(n_max, 0)))
    return blocks_raw, blocks_tgt


def _stable_target(recent_targets: list[int], value: int, days: int) -> bool:
    """True se gli ultimi `days` target (inclusi quelli passati) sono tutti == value.
    recent_targets: storia dei blocks_target dei giorni PRECEDENTI (piu' recente per
    ultimo). Con days=2 serve che ieri fosse gia' == value (piu' oggi = 2 giorni)."""
    if days <= 1:
        return True
    need = days - 1  # oggi conta come 1, servono days-1 giorni passati identici
    tail = recent_targets[-need:] if need else []
    return len(tail) >= need and all(t == value for t in tail)


@dataclass(frozen=True)
class ExposurePlan:
    sigma_hat: float | None
    scale_raw: float
    scale_applied: float
    n_max: int
    blocks_raw: float
    blocks_target: int      # target grezzo dal modello (pre-isteresi)
    blocks_to_reach: int    # cosa il sistema realizza DAVVERO oggi (post-isteresi)
    delta: int              # blocks_to_reach - blocks_current
    action: str             # open|close|hold|halt
    reason: str


def plan_exposure(
    sigma_hat: float | None,
    blocks_current: int,
    recent_targets: list[int],
    equity_eur: float,
    real_leverage: float,
    cfg: ExposureConfig,
    macro_scale: float = 1.0,
) -> ExposurePlan:
    """Calcola il piano di esposizione del giorno. PURO. Regole:
    - warm-up non pronto (sigma None) -> target 0 (§3).
    - equity sotto il floor -> HALT: chiudi tutto subito, bypassa isteresi (§2.3).
    - N_max sceso sotto i blocchi correnti -> riduzione immediata (§4.1 eccezione).
    - isteresi: gli AUMENTI attendono HYSTERESIS_DAYS di target stabile; le RIDUZIONI
      di rischio sono immediate (§4.1). delta==0 -> hold, nessun ordine.
    """
    n_max = compute_n_max(equity_eur, cfg.block_margin_eur, real_leverage, cfg.gap_tolerance)

    # Circuit breaker (§2.3): priorita' assoluta.
    if equity_eur < cfg.kill_equity_floor_eur:
        return ExposurePlan(
            sigma_hat, 0.0, 0.0, n_max, 0.0, 0, 0, -blocks_current,
            "halt", f"equity {equity_eur:.2f} < floor {cfg.kill_equity_floor_eur:.0f}: HALT")

    # Warm-up (§3): niente stima -> nessuna esposizione.
    if sigma_hat is None:
        reach = min(blocks_current, n_max)  # non aumentare, rispetta n_max
        # se n_max ha ridotto, e' comunque una riduzione (immediata)
        return ExposurePlan(
            None, 0.0, 0.0, n_max, 0.0, 0, reach, reach - blocks_current,
            "close" if reach < blocks_current else "hold",
            "warm-up non pronto (<250 barre): resto/riduco a esposizione ammessa")

    scale_raw, scale_applied = compute_scale(sigma_hat, cfg, macro_scale)
    blocks_raw, blocks_tgt = compute_target_blocks(scale_applied, n_max)

    # Se N_max e' sceso sotto i blocchi correnti, la riduzione forzata e' immediata.
    if blocks_current > n_max:
        return ExposurePlan(
            sigma_hat, scale_raw, scale_applied, n_max, blocks_raw, blocks_tgt,
            n_max, n_max - blocks_current, "close",
            f"N_max sceso a {n_max} < correnti {blocks_current}: riduzione immediata")

    if blocks_tgt == blocks_current:
        return ExposurePlan(sigma_hat, scale_raw, scale_applied, n_max, blocks_raw,
                            blocks_tgt, blocks_current, 0, "hold",
                            f"target {blocks_tgt} == correnti: nessuna azione")

    if blocks_tgt < blocks_current:
        # riduzione di rischio: immediata, bypassa isteresi (§4.1)
        return ExposurePlan(sigma_hat, scale_raw, scale_applied, n_max, blocks_raw,
                            blocks_tgt, blocks_tgt, blocks_tgt - blocks_current, "close",
                            f"riduzione {blocks_current}->{blocks_tgt}: immediata")

    # aumento: applica isteresi (§4.1)
    if _stable_target(recent_targets, blocks_tgt, cfg.hysteresis_days):
        return ExposurePlan(sigma_hat, scale_raw, scale_applied, n_max, blocks_raw,
                            blocks_tgt, blocks_tgt, blocks_tgt - blocks_current, "open",
                            f"aumento {blocks_current}->{blocks_tgt}: target stabile "
                            f"{cfg.hysteresis_days}gg, eseguo")
    return ExposurePlan(sigma_hat, scale_raw, scale_applied, n_max, blocks_raw,
                        blocks_tgt, blocks_current, 0, "hold",
                        f"aumento a {blocks_tgt} in attesa: target non ancora stabile "
                        f"{cfg.hysteresis_days}gg")
