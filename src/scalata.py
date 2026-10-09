"""La scala: la taglia segue i soldi vinti, non quelli messi.

Richiesta dell'utente del 2026-10-09: "un conto che si muove e cerca di
scalare". Fino a quel giorno nessuno dei due sistemi sul conto di prova muoveva
la taglia: il grid stava a una unita' minima fissa, l'assicurazione calcolava
tutto su 200 € dichiarati fissi. Un conto cosi' non puo' capitalizzare per
costruzione, anche quando guadagna.

La regola e' una sola, e vale per tutto il conto:

  * ogni `passo_eur` di profitto INCASSATO dall'avvio (operazioni chiuse, costi
    notturni e dividendi letti dal broker, mai il flottante) vale un gradino;
  * ogni gradino e' una taglia minima in piu' su UNO strumento, scelto fra
    quelli che hanno reso di piu' (si sale dove si vince);
  * si sale solo il lunedi'; si scende qualunque giorno, appena il profitto
    incassato non copre piu' i gradini raggiunti;
  * gli stop esistenti (per strumento, di conto, dell'assicurazione) non
    cambiano: sono loro il limite alla perdita, la scala decide solo quanto
    mettere in gioco del guadagno.

Qui c'e' solo la logica, senza rete: tutto e' riproducibile a tavolino. La
pre-registrazione e' in docs/scalata-2026-10-09.md.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"

TIPI_INCASSATI = ("TRADE", "SWAP", "CORPORATE_ACTION")


@dataclass(frozen=True)
class Regole:
    """Numeri della scala. I default sono quelli pre-registrati il 2026-10-09."""

    passo_eur: float = 5.0            # profitto incassato che vale un gradino (2,5% di 200 €)
    max_extra_per_slot: int = 1       # al massimo una taglia minima in piu' per strumento
    slot: tuple[str, ...] = ("NL25", "US100", "DE40", "J225", "GOLD", "US30", "US500", "SVXY")
    giorno_salita: int = 0            # lunedi' (datetime.weekday)

    @property
    def max_gradini(self) -> int:
        return self.max_extra_per_slot * len(self.slot)


@dataclass(frozen=True)
class Esito:
    gradino: int
    extra: dict[str, int]              # epic -> taglie minime in piu'
    profitto_netto: float
    contributi: dict[str, float]
    mancano: float                     # euro al prossimo gradino (0 se la scala e' piena)
    motivi: list[str] = field(default_factory=list)


# --- misura ----------------------------------------------------------------

def contributi_da_transazioni(transazioni: list[dict], dal_iso: str) -> dict[str, float]:
    """Quanto ha incassato ogni strumento dall'avvio dell'esercizio.

    Si contano operazioni chiuse, costi notturni e dividendi (`TIPI_INCASSATI`)
    con data `dateUtc` non precedente a `dal_iso`. Il flottante non c'e': un
    guadagno non ancora chiuso puo' sparire, e non autorizza nessun gradino.
    """
    agg: dict[str, float] = {}
    for t in transazioni or []:
        if (t.get("transactionType") or "").upper() not in TIPI_INCASSATI:
            continue
        if str(t.get("dateUtc") or "") < str(dal_iso or ""):
            continue
        try:
            imp = float(t.get("size") or 0)
        except (TypeError, ValueError):
            continue
        nome = str(t.get("instrumentName") or "?")
        agg[nome] = agg.get(nome, 0.0) + imp
    return {k: round(v, 2) for k, v in agg.items()}


def gradino_da_profitto(profitto_netto: float, regole: Regole) -> int:
    """Quanti gradini il profitto incassato copre. Mai negativo, mai oltre la scala."""
    if regole.passo_eur <= 0 or profitto_netto <= 0:
        return 0
    return min(regole.max_gradini, int(math.floor(profitto_netto / regole.passo_eur)))


def gradino_effettivo(precedente: int, calcolato: int, puo_salire: bool) -> int:
    """Si scende subito, si sale solo quando e' il giorno giusto."""
    if calcolato < precedente:
        return calcolato
    if calcolato > precedente and not puo_salire:
        return precedente
    return calcolato


def giorno_di_salita(quando: date | datetime, regole: Regole) -> bool:
    return quando.weekday() == regole.giorno_salita


def assegna(gradino: int, contributi: dict[str, float], regole: Regole) -> dict[str, int]:
    """Dove vanno i gradini: una taglia in piu' per volta, dallo strumento che ha
    reso di piu' in giu'. Solo chi ha incassato qualcosa riceve un gradino: con
    profitto zero o negativo non si sale per definizione. Un gradino che non
    trova uno strumento resta inutilizzato: la scala non inventa candidati.
    """
    extra = {e: 0 for e in regole.slot}
    if gradino <= 0:
        return extra
    candidati = sorted((e for e in regole.slot if contributi.get(e, 0.0) > 0),
                       key=lambda e: (-contributi.get(e, 0.0), regole.slot.index(e)))
    restanti = gradino
    for _ in range(regole.max_extra_per_slot):
        for e in candidati:
            if restanti <= 0:
                break
            if extra[e] < regole.max_extra_per_slot:
                extra[e] += 1
                restanti -= 1
    return extra


def riduci(extra_prec: dict[str, int], quanti: int,
           contributi: dict[str, float], regole: Regole) -> dict[str, int]:
    """Toglie `quanti` taglie in piu', partendo da chi ha reso meno.

    Serve nei giorni in cui si scende: non si ridistribuisce tutto da capo
    (sposterebbe la taglia da uno strumento all'altro pagando spread due volte),
    si toglie e basta.
    """
    extra = {e: max(0, int(extra_prec.get(e, 0) or 0)) for e in regole.slot}
    for _ in range(max(0, quanti)):
        portatori = [e for e in regole.slot if extra[e] > 0]
        if not portatori:
            break
        peggiore = min(portatori, key=lambda e: (contributi.get(e, 0.0),
                                                  -regole.slot.index(e)))
        extra[peggiore] -= 1
    return extra


def valuta(transazioni: list[dict], dal_iso: str, precedente: int,
           puo_salire: bool, regole: Regole | None = None,
           extra_precedente: dict[str, int] | None = None) -> Esito:
    """Il giro completo della scala, senza I/O: misura, gradino, assegnazione.

    `puo_salire` lo decide il chiamante (il cron del lunedi' mattina): qui si
    applica soltanto. Scendere e' sempre permesso. Nei giorni senza salita la
    distribuzione resta quella che era (meno i gradini persi): si ridistribuisce
    da capo solo quando si puo' salire, cioe' una volta a settimana.
    """
    regole = regole or Regole()
    contributi = contributi_da_transazioni(transazioni, dal_iso)
    profitto = round(sum(contributi.values()), 2)
    calcolato = gradino_da_profitto(profitto, regole)
    salita = bool(puo_salire)
    gradino = gradino_effettivo(precedente, calcolato, salita)

    prec_extra = {e: max(0, int((extra_precedente or {}).get(e, 0) or 0))
                  for e in regole.slot}
    if salita or sum(prec_extra.values()) != precedente:
        # lunedi' (o stato incoerente): si ridistribuisce da capo
        extra = assegna(gradino, contributi, regole)
        assegnati = sum(extra.values())
        if assegnati < gradino:
            # meno strumenti in utile che gradini: la scala si ferma a quelli
            gradino = assegnati
    elif gradino < precedente:
        extra = riduci(prec_extra, precedente - gradino, contributi, regole)
    else:
        extra = prec_extra

    motivi: list[str] = []
    if calcolato > precedente and not salita:
        motivi.append(f"il profitto coprirebbe {calcolato} gradini, ma si sale solo "
                      f"il lunedì: resto a {gradino}")
    if gradino < precedente:
        motivi.append(f"il profitto incassato ({profitto:+.2f} €) non copre più "
                      f"{precedente} gradini: scendo a {gradino}")
    if gradino > precedente:
        motivi.append(f"il profitto incassato ({profitto:+.2f} €) copre {gradino} "
                      f"gradini da {regole.passo_eur:.0f} €: salgo")

    if gradino >= regole.max_gradini:
        mancano = 0.0
    else:
        mancano = round(max(0.0, (gradino + 1) * regole.passo_eur - profitto), 2)
    return Esito(gradino=gradino, extra=extra, profitto_netto=profitto,
                 contributi=contributi, mancano=mancano, motivi=motivi)


# --- stato su file -----------------------------------------------------------
# I job del grid e dell'assicurazione leggono da qui la taglia in piu'. Un file
# assente o rotto vale "nessun gradino": la scala non puo' mai AUMENTARE una
# taglia per errore di lettura.

def _file(env: str) -> Path:
    return DATA / f"scalata_{env}.json"


def leggi(env: str = "demo") -> dict:
    try:
        return json.loads(_file(env).read_text())
    except Exception:
        return {}


def scrivi(env: str, st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    _file(env).write_text(json.dumps(st, indent=1, ensure_ascii=False))


def extra_per(epic: str, env: str = "demo") -> int:
    """Taglie minime in piu' per questo strumento, adesso. 0 se non si sa."""
    try:
        v = int((leggi(env).get("extra") or {}).get(epic, 0))
    except (TypeError, ValueError):
        return 0
    return max(0, v)


def moltiplicatore(epic: str, env: str = "demo") -> int:
    """Per quanto moltiplicare la taglia minima: 1 = come sempre."""
    return 1 + extra_per(epic, env)


def aggiorna_stato(env: str, esito: Esito, regole: Regole, capitale: float,
                   quando: datetime | None = None) -> dict:
    """Scrive lo stato nuovo e tiene la storia dei cambi di gradino."""
    quando = quando or datetime.now(timezone.utc)
    prec = leggi(env)
    storia = list(prec.get("storia") or [])
    if esito.gradino != int(prec.get("gradino") or 0) or not prec:
        storia.append({"quando": quando.isoformat(timespec="seconds"),
                       "da": int(prec.get("gradino") or 0), "a": esito.gradino,
                       "profitto_netto": esito.profitto_netto,
                       "motivo": "; ".join(esito.motivi) or "prima misura"})
    st = {
        "aggiornato": quando.isoformat(timespec="seconds"),
        "capitale": capitale,
        "passo_eur": regole.passo_eur,
        "max_gradini": regole.max_gradini,
        "gradino": esito.gradino,
        "profitto_netto": esito.profitto_netto,
        "mancano": esito.mancano,
        "contributi": esito.contributi,
        "extra": esito.extra,
        "storia": storia[-100:],
    }
    scrivi(env, st)
    return st
