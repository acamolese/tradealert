"""Selezione dinamica dello strumento per il controller v2 (2026-08-14).

Perche' esiste: v2 aveva l'epic hardcoded (V2_EPIC=US500) e ignorava il tabellone
che lo spinner produce ogni notte su 700 strumenti. Al 2026-08-13 US500 era 14o su
23 per deriva attesa: il sistema reale teneva lo strumento peggiore tra quelli
disponibili, e non aveva modo di accorgersene.

Metrica: net_adj / sigma, non la g del tabellone. La g dello spinner e' calcolata
alla taglia eseguibile con l'equity DEMO e il suo cap di leva; v2 dimensiona da se'
con volatility targeting, quindi cio' che gli serve e' il rapporto premio/rischio
per unita' di volatilita', che e' indipendente dalla taglia. Usare la g importerebbe
la meccanica di sizing di un altro sistema su un conto diverso.

Principio: le azioni di RISCHIO precedono quelle di OTTIMIZZAZIONE. Lo switch si
valuta solo quando il piano del giorno e' 'hold' (nessuna apertura/chiusura
pendente per volatilita'), e non cambia l'esposizione: sostituisce n blocchi su A
con n blocchi su B. Non essendo un aumento di rischio, non passa dall'isteresi sui
blocchi, che serve a evitare churn sul LIVELLO di esposizione.

Logica PURA: nessun I/O. Il job orchestra.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    epic: str
    net_adj: float
    sigma: float

    @property
    def score(self) -> float:
        """Premio netto per unita' di volatilita' (Sharpe atteso ex-ante)."""
        if self.sigma is None or self.sigma <= 0:
            return float("-inf")
        return self.net_adj / self.sigma


def rank_candidates(rows: list[dict], executable: set[str]) -> list[Candidate]:
    """Candidati ordinati per score desc. rows = righe odds_board eligible.
    Filtra: solo long (v2 e' solo-long per design), solo epic eseguibili sul conto
    reale alla taglia di un blocco, solo net_adj e sigma validi."""
    out: list[Candidate] = []
    for r in rows:
        if (r.get("side") or "").lower() != "long":
            continue
        epic = r.get("epic")
        if not epic or epic not in executable:
            continue
        try:
            net_adj = float(r.get("net_adj"))
            sigma = float(r.get("sigma_ann"))
        except (TypeError, ValueError):
            continue
        if sigma <= 0 or net_adj <= 0:
            continue
        out.append(Candidate(epic, net_adj, sigma))
    out.sort(key=lambda c: c.score, reverse=True)
    return out


@dataclass(frozen=True)
class SwitchDecision:
    switch: bool
    from_epic: str
    to_epic: str | None
    reason: str
    edge: float = 0.0        # miglioramento relativo dello score


def decide_switch(
    current_epic: str,
    candidates: list[Candidate],
    picks_history: list[str],
    *,
    plan_action: str,
    board_age_days: int | None,
    enabled: bool,
    min_edge: float,
    stable_days: int,
    board_max_age_days: int,
) -> SwitchDecision:
    """Decide se sostituire lo strumento. Fail-closed su ogni incertezza.

    picks_history: il miglior candidato dei giorni PRECEDENTI (piu' recente per
    ultimo), per l'isteresi di switch. Serve che lo stesso epic sia in testa da
    `stable_days` giorni (oggi incluso) prima di muovere denaro.
    """
    if not enabled:
        return SwitchDecision(False, current_epic, None, "switch disabilitato (flag off)")
    if plan_action != "hold":
        return SwitchDecision(False, current_epic, None,
                              f"piano='{plan_action}': le azioni di rischio precedono lo switch")
    if board_age_days is None:
        return SwitchDecision(False, current_epic, None, "tabellone assente: nessuno switch")
    if board_age_days > board_max_age_days:
        return SwitchDecision(False, current_epic, None,
                              f"tabellone vecchio di {board_age_days}gg "
                              f"(max {board_max_age_days}): nessuno switch")
    if not candidates:
        return SwitchDecision(False, current_epic, None,
                              "nessun candidato eseguibile: nessuno switch")

    best = candidates[0]
    if best.epic == current_epic:
        return SwitchDecision(False, current_epic, None,
                              f"{current_epic} e' gia' il migliore (score {best.score:.3f})")

    cur = next((c for c in candidates if c.epic == current_epic), None)
    if cur is None:
        # lo strumento detenuto non e' piu' nel set eligible: non e' un caso di
        # ottimizzazione ma di deterioramento. Serve comunque l'isteresi, per non
        # inseguire un tabellone rumoroso con denaro reale.
        edge = float("inf")
        base = f"{current_epic} non e' piu' eligible"
    else:
        if cur.score <= 0:
            edge = float("inf")
        else:
            edge = (best.score - cur.score) / abs(cur.score)
        if edge < min_edge:
            return SwitchDecision(False, current_epic, None,
                                  f"{best.epic} batte {current_epic} solo del {edge:.1%} "
                                  f"(serve {min_edge:.0%}): non vale il costo di giro", edge)
        base = f"{best.epic} batte {current_epic} del {edge:.1%}"

    need = stable_days - 1          # oggi conta come 1
    tail = picks_history[-need:] if need > 0 else []
    if need > 0 and (len(tail) < need or any(p != best.epic for p in tail)):
        return SwitchDecision(False, current_epic, None,
                              f"{base}, ma non e' in testa da {stable_days}gg "
                              f"(storia: {picks_history[-stable_days:] or 'vuota'})", edge)

    return SwitchDecision(True, current_epic, best.epic,
                          f"{base}, stabile da {stable_days}gg: sostituisco", edge)
