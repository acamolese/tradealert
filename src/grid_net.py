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


def ancora_mobile(closes: list[float], periodo: int) -> float | None:
    """Ancoraggio = media mobile esponenziale del prezzo.

    FIX 2026-08-21. Con ancoraggio FISSO al prezzo di partenza, un mercato che
    sale e non torna indietro porta il grid al tetto dello scoperto e ce lo
    lascia: misurato su 400 giorni, GOLD 98.5% del tempo short, US100 98.5%,
    US500 97.8%, con correlazione -0.97 tra deriva dell'asset e posizione media.
    Non era un grid, era una scommessa fissa contro la tendenza.

    Con l'ancoraggio mobile il riferimento segue la tendenza: il grid scommette
    sul ritorno verso una media che si MUOVE, non verso un punto del passato.
    Su trend persistenti la posizione resta vicina allo zero invece di incollarsi
    al tetto; prende posizione solo sugli scostamenti dalla media.
    """
    if not closes or periodo <= 1:
        return closes[-1] if closes else None
    k = 2.0 / (periodo + 1.0)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema


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
