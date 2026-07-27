"""Formule dell'odds board (spec §3.1). Logica PURA: nessun prezzo, nessun segnale
di direzione. La direzione e' decisa dal segno di net_adj (premio dichiarato +
financing misurato), niente altro (§3.2).

Fondamento: crescita geometrica a leva f  g(f) = f*net_adj - f^2*sigma^2/2. Il drag
cresce col quadrato della leva. La zona giocabile e' f <= 2*net_adj/sigma^2.
"""
from __future__ import annotations

from dataclasses import dataclass


def net_long(mu_total: float, fin_long: float) -> float:
    """Deriva netta long sul nozionale. fin = tasso PAGATO (negativo = ricevuto)."""
    return mu_total - fin_long


def net_short(mu_total: float, fin_short: float) -> float:
    return -mu_total - fin_short


def spread_ann(spread_bps: float, holding_days: int) -> float:
    """Ammortamento di un giro andata/ritorno sul periodo di detenzione (§3.1)."""
    if holding_days <= 0:
        return 0.0
    return (spread_bps / 10000.0) * (365.0 / holding_days)


def g_of_f(f: float, net_adj: float, sigma: float) -> float:
    return f * net_adj - f * f * sigma * sigma / 2.0


def executable_f(f_opt: float | None, min_notional_eur: float, equity: float,
                 f_max_pos: float) -> tuple[int | None, float | None]:
    """(n_unita', f_exec) piu' vicina a f_opt con n*minDealSize, f_exec <= F_MAX_POS.
    Ritorna (None, None) se nemmeno la taglia minima sta nella zona giocabile."""
    if equity <= 0 or min_notional_eur <= 0:
        return None, None
    f_unit = min_notional_eur / equity          # f con 1 unita' (minDealSize)
    if f_unit > f_max_pos:
        return None, None                        # not_executable
    n_max = int(f_max_pos * equity / min_notional_eur)
    n_opt = (f_opt * equity / min_notional_eur) if (f_opt and f_opt > 0) else 1.0
    n = max(1, min(round(n_opt), max(n_max, 1)))
    return n, n * f_unit


@dataclass(frozen=True)
class SideEval:
    net: float
    net_adj: float
    f_opt: float | None
    f_exec: float | None
    units: int | None
    g_exec: float | None
    status: str          # eligible|below_gmin|not_executable|not_rated|excluded


def evaluate_side(
    side: str, mu_total: float, fin: float, sigma_ann: float | None,
    spread_bps: float, min_notional_eur: float, equity: float,
    holding_days: int, f_max_pos: float, g_min: float,
    asset_class: str = "",
) -> SideEval:
    """Valuta uno strumento-verso. status:
    - excluded: classe non mappata
    - not_rated: sigma non disponibile (warm-up insufficiente)
    - not_executable: nemmeno la taglia minima sta nella zona giocabile
    - below_gmin: eseguibile ma g_exec < G_MIN (o net_adj <= 0)
    - eligible: net_adj > 0, f_exec esiste, g_exec >= G_MIN
    """
    if asset_class == "excluded":
        return SideEval(0, 0, None, None, None, None, "excluded")
    if sigma_ann is None or sigma_ann <= 0:
        return SideEval(0, 0, None, None, None, None, "not_rated")

    net = net_long(mu_total, fin) if side == "long" else net_short(mu_total, fin)
    net_adj = net - spread_ann(spread_bps, holding_days)

    if net_adj <= 0:
        # verso non giocabile: nessuna leva positiva rende g>0
        return SideEval(net, net_adj, None, None, None, None, "below_gmin")

    f_opt = net_adj / (sigma_ann * sigma_ann)
    units, f_exec = executable_f(f_opt, min_notional_eur, equity, f_max_pos)
    if units is None:
        return SideEval(net, net_adj, f_opt, None, None, None, "not_executable")

    g_exec = g_of_f(f_exec, net_adj, sigma_ann)
    status = "eligible" if g_exec >= g_min else "below_gmin"
    return SideEval(net, net_adj, f_opt, f_exec, units, g_exec, status)
