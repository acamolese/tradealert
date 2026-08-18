"""Grid trader: logica PURA (nessun I/O). Il job orchestra.

Il grid non predice la direzione: mette una scaletta di livelli geometrici e
compra quando il prezzo scende di un gradino, rivende quando risale di un
gradino. Guadagna dall'oscillazione, perde quando il prezzo se ne va e non torna
(inventory risk). Validato su BTCUSD 1000 giorni: 12/15 finestre mobili positive
per il verso long a passo 2% (jobs/grid_backtest.py).

Lo STATO NON viene persistito: si deriva dalle posizioni aperte sul broker, che e'
l'unica fonte di verita'. E' una scelta deliberata dopo il disallineamento del
2026-08-17 sul demo, dove il DB dava per chiuse tre posizioni ancora aperte.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def livello(prezzo: float, p0: float, step: float) -> int:
    """Indice del gradino geometrico che contiene `prezzo`, ancorato a p0."""
    if prezzo <= 0 or p0 <= 0 or step <= 0:
        return 0
    return math.floor(math.log(prezzo / p0) / math.log(1.0 + step))


@dataclass(frozen=True)
class Azione:
    tipo: str          # "apri" | "chiudi" | "nulla"
    deal_id: str | None
    livello: int
    motivo: str


@dataclass(frozen=True)
class Piano:
    livello_corrente: int
    livelli_aperti: list[int]
    azioni: list[Azione]
    pnl_aperto: float
    motivo: str


def pianifica(
    prezzo: float,
    p0: float,
    step: float,
    posizioni: list[dict],
    *,
    side: str,
    max_posizioni: int,
    kill_pnl_eur: float,
    equity: float,
    kill_equity_eur: float,
) -> Piano:
    """Decide cosa fare adesso. `posizioni` = [{deal_id, prezzo_apertura, pnl}].

    Regole (verso long; short e' speculare):
    - il prezzo scende di un gradino non coperto -> APRE una unita';
    - una unita' e' in guadagno di almeno un gradino pieno -> CHIUDE;
    - mai piu' di max_posizioni unita' aperte insieme;
    - kill switch su perdita aggregata o su equity sotto il floor: chiude tutto e
      non riapre (fail-closed, ha priorita' su ogni altra regola).
    """
    k = livello(prezzo, p0, step)
    aperti = sorted(livello(p["prezzo_apertura"], p0, step) for p in posizioni)
    pnl = sum(float(p.get("pnl") or 0) for p in posizioni)

    if equity < kill_equity_eur:
        return Piano(k, aperti,
                     [Azione("chiudi", p["deal_id"], livello(p["prezzo_apertura"], p0, step),
                             "KILL equity") for p in posizioni],
                     pnl, f"KILL: equity {equity:.2f}€ < floor {kill_equity_eur:.2f}€")
    if pnl < -abs(kill_pnl_eur):
        return Piano(k, aperti,
                     [Azione("chiudi", p["deal_id"], livello(p["prezzo_apertura"], p0, step),
                             "KILL perdita") for p in posizioni],
                     pnl, f"KILL: perdita aperta {pnl:.2f}€ oltre {-abs(kill_pnl_eur):.2f}€")

    azioni: list[Azione] = []

    # Il confronto e' sui PREZZI, non sui livelli interi.
    # Bug del 2026-08-18 (colto su denaro reale prima che facesse danni): il
    # gradino corrente si calcola sul MID, ma il broker registra l'apertura al
    # prezzo ASK (long). Con p0=64589.75 la prima unita' e' finita a 64613.85,
    # cioe' al gradino 0, mentre il mid stava gia' al gradino -1: il livello -1
    # risultava "scoperto" e il grid avrebbe riaperto allo stesso prezzo a ogni
    # run, fino al cap. Confrontare i prezzi rende la regola immune allo spread e
    # coincide con il backtest: si compra un gradino sotto l'ULTIMO acquisto.
    if side == "long":
        soglia_apertura = (min(p["prezzo_apertura"] for p in posizioni) * (1 - step)
                           if posizioni else p0 * (1 - step))
        apri = prezzo <= soglia_apertura
    else:
        soglia_apertura = (max(p["prezzo_apertura"] for p in posizioni) * (1 + step)
                           if posizioni else p0 * (1 + step))
        apri = prezzo >= soglia_apertura

    # 1) chiusure: una unita' si chiude quando ha guadagnato un gradino pieno
    #    rispetto al SUO prezzo di carico (spread di entrata gia' incluso).
    for p in posizioni:
        c = p["prezzo_apertura"]
        obiettivo = c * (1 + step) if side == "long" else c * (1 - step)
        raggiunto = prezzo >= obiettivo if side == "long" else prezzo <= obiettivo
        if raggiunto:
            azioni.append(Azione("chiudi", p["deal_id"], livello(c, p0, step),
                                 f"carico {c:.2f} -> {prezzo:.2f} "
                                 f"(obiettivo {obiettivo:.2f})"))

    # 2) apertura: una sola per run, e solo nella direzione favorevole.
    restanti = len(posizioni) - len(azioni)
    if apri and restanti < max_posizioni:
        azioni.append(Azione("apri", None, k,
                             f"prezzo {prezzo:.2f} <= soglia {soglia_apertura:.2f}"
                             if side == "long" else
                             f"prezzo {prezzo:.2f} >= soglia {soglia_apertura:.2f}"))

    motivo = "; ".join(a.motivo for a in azioni) if azioni else "nessun gradino attraversato"
    return Piano(k, aperti, azioni, pnl, motivo)
