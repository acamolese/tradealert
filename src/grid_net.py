"""Grid a POSIZIONE NETTA (netting). Logica pura.

Il conto Capital e' in netting: aprire uno short mentre si e' long non crea una
seconda posizione, RIDUCE la prima (verificato il 2026-08-19 su GOLD: long + short
di pari size -> zero posizioni). Invece di essere un limite e' l'occasione per il
grid vero, bidirezionale:

    target_unita = -livello(prezzo)      (cappato a +-max_unita)

Sotto l'ancoraggio si e' LONG, sopra si e' SHORT, e la size netta cresce di una
unita' per ogni gradino. Il prezzo che scende fa comprare, quello che sale fa
vendere: ogni oscillazione incassa il passo, in ENTRAMBE le direzioni. Il modello
a posizioni separate copriva un solo verso e stava fermo meta' del tempo.

Un solo ordine per run (la differenza tra target e posizione attuale), quindi
niente stato da riconciliare: la posizione netta del broker E' lo stato.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def livello(prezzo: float, p0: float, step: float) -> int:
    if prezzo <= 0 or p0 <= 0 or step <= 0:
        return 0
    return math.floor(math.log(prezzo / p0) / math.log(1.0 + step))


def target_unita(prezzo: float, p0: float, step: float, max_unita: int) -> int:
    """Unita' nette desiderate: positive = long, negative = short."""
    k = livello(prezzo, p0, step)
    # il gradino 0 e' la banda [p0, p0*(1+step)): dentro la banda si sta flat
    return max(-max_unita, min(max_unita, -k))


@dataclass(frozen=True)
class PianoNet:
    livello: int
    unita_correnti: float
    unita_target: int
    delta: float
    azione: str          # "compra" | "vendi" | "nulla" | "kill"
    motivo: str


def pianifica_net(
    prezzo: float,
    p0: float,
    step: float,
    unita_correnti: float,
    *,
    max_unita: int,
    pnl_aperto: float,
    kill_pnl_eur: float,
    equity: float,
    kill_equity_eur: float,
) -> PianoNet:
    """Un solo ordine per run. Il kill switch azzera la posizione e ha priorita'."""
    k = livello(prezzo, p0, step)

    if equity < kill_equity_eur:
        return PianoNet(k, unita_correnti, 0, -unita_correnti, "kill",
                        f"equity {equity:.2f}€ sotto il floor {kill_equity_eur:.0f}€")
    if pnl_aperto < -abs(kill_pnl_eur):
        return PianoNet(k, unita_correnti, 0, -unita_correnti, "kill",
                        f"perdita aperta {pnl_aperto:.2f}€ oltre il limite "
                        f"{-abs(kill_pnl_eur):.2f}€")

    tgt = target_unita(prezzo, p0, step, max_unita)
    delta = tgt - unita_correnti
    if abs(delta) < 0.5:                      # meno di mezza unita': non vale il giro
        return PianoNet(k, unita_correnti, tgt, 0.0, "nulla",
                        f"gradino {k}: gia' a target ({unita_correnti:+.0f} unita')")
    azione = "compra" if delta > 0 else "vendi"
    return PianoNet(k, unita_correnti, tgt, delta, azione,
                    f"gradino {k}: da {unita_correnti:+.0f} a {tgt:+.0f} unita'")
