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


def barre_chiuse(prices: list[dict], oggi_utc: str) -> list[float]:
    """Close medi (bid+ask)/2 delle sole barre DAY gia' CHIUSE.

    Capital include nella risposta la barra del giorno in corso (verificato il
    03/09: l'ultima barra ha la data di oggi). Metterla nell'EMA fa muovere
    l'ancoraggio a ogni run di 10 minuti, inseguendo il prezzo che dovrebbe
    misurare: i livelli del grid si spostano di continuo e i giri si accorciano.
    Escludendola i livelli restano fermi per tutta la giornata.
    """
    out = []
    for x in prices:
        if (x.get("snapshotTimeUTC") or "")[:10] >= oggi_utc[:10]:
            continue
        cp = x.get("closePrice") or {}
        if cp.get("bid") and cp.get("ask"):
            out.append((float(cp["bid"]) + float(cp["ask"])) / 2)
    return out


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


def target_banda(prezzo: float, p0: float, entrata: float, uscita: float,
                 passo: float, unita_correnti: float, max_unita: int) -> int:
    """Zona morta, passo e isteresi come TRE parametri distinti (2026-09-07).

    Nelle versioni precedenti erano lo stesso numero, e la banda del gradino
    zero partiva dall'ancoraggio invece di stare a cavallo: misurato sui log dal
    22/08, 28.774 rilevamenti, il sistema e' stato LONG il 59% del tempo e SHORT
    lo 0,00%, perche' per andare short serviva un prezzo del 3% sopra la media a
    5 giorni, che non e' mai accaduto (99esimo percentile: +2,25%).

    Qui:
      - dentro +-``entrata`` si sta fermi (zona morta, simmetrica);
      - oltre, si prende posizione CONTRO lo scostamento, una unita' in piu'
        ogni ``passo``;
      - si torna flat solo quando il prezzo rientra oltre ``uscita``, che e'
        piu' vicino all'ancora dell'entrata: senza questa distanza il sistema
        entra ed esce sul confine pagando solo spread (79-83% dei fill reali
        distava meno dello 0,15% dal precedente).

    ``entrata``, ``uscita`` e ``passo`` sono frazioni (0.0075 = 0,75%).
    """
    if prezzo <= 0 or p0 <= 0 or entrata <= 0 or passo <= 0:
        return 0
    x = prezzo / p0 - 1.0
    u = int(round(unita_correnti))

    if u > 0:                                   # long: si esce risalendo
        if x >= -uscita:
            return 0
        return min(max_unita, 1 + int((-x - entrata) / passo)) if x <= -entrata else u
    if u < 0:                                   # short: si esce scendendo
        if x <= uscita:
            return 0
        return -min(max_unita, 1 + int((x - entrata) / passo)) if x >= entrata else u

    if x <= -entrata:
        return min(max_unita, 1 + int((-x - entrata) / passo))
    if x >= entrata:
        return -min(max_unita, 1 + int((x - entrata) / passo))
    return 0


def target_isteresi(prezzo: float, p0: float, step: float,
                    unita_correnti: float, max_unita: int) -> int:
    """Unita' nette desiderate con ISTERESI da grid classico (fix 2026-09-03).

    Con `target_unita` la prima unita' long entrava appena SOTTO p0 e usciva
    appena SOPRA p0: le due soglie coincidevano, e il passo del 3% proteggeva
    solo la seconda unita'. Misurato sui log dal 22/08: il 79% (reale) e l'83%
    (demo) dei fill consecutivi distava meno dello 0.15% dal precedente, 1 su
    250 oltre l'1%. Il grid non incassava il passo, incassava rumore meno lo
    spread (13 giorni: reale -0.03€ su 129 movimenti).

    ESITO DEL BACKTEST INTRADAY (jobs/grid_intraday_test.py, 180 giorni a 15
    minuti, calibrato sui fill reali): il chattering NON e' una perdita. La
    logica `target_unita` rende +0.39€ per finestra di 30 giorni alla taglia
    reale (68% finestre positive), questa +0.35€ (48%): giri rari e risultato
    dominato dal mark-to-market. Resta disponibile, spenta di default.

    Qui si compra sul livello INFERIORE e si vende su quello SUPERIORE: con u
    unita' long si aggiunge solo se il prezzo scende sotto p0*(1+s)^-(u+1) e si
    riduce solo se risale sopra p0*(1+s)^-(u-1). La prima unita' entra a -1
    passo ed esce a p0: un giro vale un passo intero. Simmetrico per lo short.
    Lo stato resta la posizione del broker (u), nessun file.
    """
    if prezzo <= 0 or p0 <= 0 or step <= 0:
        return 0
    u = int(round(unita_correnti))
    x = math.log(prezzo / p0) / math.log(1.0 + step)   # posizione frazionaria
    t = u
    if u >= 0:
        while t < max_unita and x < -(t + 1):
            t += 1
        if t == u:
            while t > 0 and x > -(t - 1):
                t -= 1
    if u <= 0 and t == u:
        while -t < max_unita and x > (-t + 1):
            t -= 1
        if t == u:
            while t < 0 and x < (-t - 1):
                t += 1
    return max(-max_unita, min(max_unita, t))


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
    isteresi: bool = False,
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

    if isteresi:
        tgt = target_isteresi(prezzo, p0, step, unita_correnti, max_unita)
    else:
        tgt = target_unita(prezzo, p0, step, max_unita)
    delta = tgt - unita_correnti
    if abs(delta) < 0.5:                      # meno di mezza unita': non vale il giro
        return PianoNet(k, unita_correnti, tgt, 0.0, "nulla",
                        f"gradino {k}: gia' a target ({unita_correnti:+.0f} unita')")
    azione = "compra" if delta > 0 else "vendi"
    return PianoNet(k, unita_correnti, tgt, delta, azione,
                    f"gradino {k}: da {unita_correnti:+.0f} a {tgt:+.0f} unita'")
