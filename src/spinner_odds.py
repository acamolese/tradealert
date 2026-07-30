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


# --- costruzione del portafoglio (§4 + constants_log F_BUDGET_POS 2026-07-30) ---

def portfolio_size(f_unit: float, f_budget: float, remaining: float,
                   f_max_pos: float, net_adj: float, sigma: float,
                   g_min: float) -> tuple[int, float, float] | None:
    """Taglia di UNA posizione dentro il portafoglio: n unita' ~ f_budget/f_unit,
    cap al budget aggregato residuo e a f_max_pos. Eccezione ticket lumpy: 1 unita'
    anche se f_unit > f_budget, purche' stia nei cap. Se la g alla taglia scelta
    e' sotto g_min, aumenta n finche' i cap lo consentono (g cresce fino a f_opt,
    che per gli eligible e' sempre oltre f_max_pos). Ritorna (n, f, g) o None."""
    if f_unit <= 0:
        return None
    f_cap = min(f_max_pos, remaining)
    if f_unit > f_cap + 1e-9:
        return None                       # nemmeno la taglia minima sta nei cap
    n = max(1, round(f_budget / f_unit))
    while n * f_unit > f_cap + 1e-9:
        n -= 1
    g = g_of_f(n * f_unit, net_adj, sigma)
    while g < g_min and (n + 1) * f_unit <= f_cap + 1e-9:
        n += 1
        g = g_of_f(n * f_unit, net_adj, sigma)
    if g < g_min:
        return None
    return n, n * f_unit, g


def select_target(
    elig: list[dict], held_keys: set[tuple[str, str]],
    consec: dict[tuple[str, str], int], equity: float, *,
    max_positions: int, max_per_class: int, f_max_account: float,
    f_max_pos: float, f_budget: float, g_min: float, entry_confirm_scans: int,
) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Selezione §4 con budget per posizione. elig = righe board eligible
    (epic, side, asset_class, min_notional_eur, net_adj, sigma_ann) ordinate per
    g_exec desc. Le posizioni GIA' in target (held_keys) hanno priorita' e saltano
    l'isteresi (continuita' buy&hold: niente crowd-out da un nuovo rank).
    Ritorna (scelte con units/f_chosen/g_chosen/reason, scarti con motivo)."""
    ordered = ([e for e in elig if (e["epic"], e["side"]) in held_keys]
               + [e for e in elig if (e["epic"], e["side"]) not in held_keys])
    chosen: list[dict] = []
    rejects: list[tuple[str, str, str]] = []
    per_class: dict[str, int] = {}
    epics_taken: set[str] = set()
    f_sum = 0.0
    for e in ordered:
        key = (e["epic"], e["side"])
        held = key in held_keys
        if len(chosen) >= max_positions:
            rejects.append((e["epic"], e["side"], "MAX_POSITIONS"))
            continue
        if e["epic"] in epics_taken:      # mai due versi dello stesso epic
            rejects.append((e["epic"], e["side"], "EPIC_DOPPIO"))
            continue
        cls = e["asset_class"]
        if per_class.get(cls, 0) >= max_per_class:
            rejects.append((e["epic"], e["side"], f"MAX_PER_CLASS[{cls}]"))
            continue
        f_unit = float(e["min_notional_eur"] or 0) / equity if equity > 0 else 0.0
        sized = portfolio_size(f_unit, f_budget, f_max_account - f_sum, f_max_pos,
                               float(e["net_adj"]), float(e["sigma_ann"]), g_min)
        if sized is None:
            rejects.append((e["epic"], e["side"],
                            f"BUDGET_F[unit {f_unit:.2f}, residuo {f_max_account - f_sum:.2f}]"
                            if f_unit > min(f_max_pos, f_max_account - f_sum)
                            else "G_MIN_TAGLIA"))
            continue
        if not held and consec.get(key, 0) + 1 < entry_confirm_scans:
            rejects.append((e["epic"], e["side"],
                            f"ISTERESI[{consec.get(key, 0) + 1}/{entry_confirm_scans}]"))
            continue
        n, f, g = sized
        chosen.append({**e, "units": n, "f_chosen": round(f, 3),
                       "g_chosen": round(g, 5), "reason": "hold" if held else "enter"})
        per_class[cls] = per_class.get(cls, 0) + 1
        epics_taken.add(e["epic"])
        f_sum += f
    return chosen, rejects
