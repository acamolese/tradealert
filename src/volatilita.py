"""Vendita di assicurazione sulla volatilita': le decisioni, senza I/O.

Qui sta solo la logica: quanta esposizione tenere, che taglia ordinare, dove
mettere lo stop, quando fermarsi. Niente rete, niente file, niente broker, cosi'
e' testabile e ogni decisione e' riproducibile a tavolino.

Il sistema non prevede niente: sta corto su uno strumento costruito per perdere
valore e incassa quel decadimento. L'unica cosa che modula e' QUANTA esposizione
tenere, e lo fa su condizioni misurabili (pendenza della curva, livello del VIX,
storia del conto), mai su una previsione di prezzo.

Regola di sicurezza che attraversa tutto il modulo: un dato mancante non
autorizza mai un aumento e non fa mai scattare una protezione. Se non sappiamo,
si resta al gradino base e non si tocca nulla.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Segnale:
    """Stato del mercato della volatilita' in un istante.

    vix              livello dell'indice di volatilita' (VIX spot)
    vixm             ETF sui futures a medio termine, il proxy di Capital per la
                     parte lunga della curva
    pendenza_mediana mediana del rapporto vixm/vix nell'ultimo anno
    percentile       posizione del VIX nella sua storia recente, 0-100

    Perche' serve la mediana. VIXM e' un ETF, non un indice: il suo PREZZO non
    e' confrontabile con il LIVELLO del VIX, e per costruzione scivola nel tempo
    (mediana del rapporto: 1.79 nel 2021, 0.84 nel 2025, 0.77 nel 2026). Una
    soglia fissa sul rapporto grezzo misurerebbe il decadimento dell'ETF, non lo
    stato della curva. Quello che conta e' se oggi il rapporto e' alto o basso
    RISPETTO AL SUO PASSATO RECENTE, ed e' quello che misura `pendenza_relativa`.
    """

    vix: float | None = None
    vixm: float | None = None
    percentile: float | None = None
    pendenza_mediana: float | None = None

    @property
    def pendenza(self) -> float | None:
        """Rapporto grezzo vixm/vix. Utile solo per i log: non ha soglie sensate."""
        if not self.vix or not self.vixm:
            return None
        return self.vixm / self.vix

    @property
    def pendenza_relativa(self) -> float | None:
        """Quanto la curva e' ripida oggi rispetto all'ultimo anno.

        1.00 = come al solito. Sotto 0.90 la parte corta della curva sta
        correndo piu' della lunga, cioe' il mercato ha paura adesso: nei dati
        Capital 2025-2026 tutte le settimane con VIX sopra 24 stanno sotto 0.87.
        """
        grezza = self.pendenza
        if grezza is None or not self.pendenza_mediana:
            return None
        return grezza / self.pendenza_mediana

    @property
    def completo(self) -> bool:
        """Senza la mediana storica il segnale non e' interpretabile."""
        return (self.vix is not None and self.vixm is not None
                and bool(self.pendenza_mediana))


@dataclass(frozen=True)
class Conto:
    """Quello che sappiamo della storia del sistema, non del mercato."""

    giorni_operativi: int = 0
    risultato_cumulato_eur: float = 0.0
    giorni_da_ultimo_stop: int | None = None   # None = nessuno stop mai
    in_pausa: bool = False


@dataclass(frozen=True)
class Gradino:
    """Esito della scala: quanta esposizione, con che nome e perche'."""

    nome: str
    frazione: float
    motivi: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Parametri:
    """Configurazione della strategia. I default replicano l'operativita'
    in produzione dal 2026-09-07 (esposizione fissa al 15%, tetto 25%)."""

    epic: str = "UVXY"
    capitale_default: float = 200.0

    # scala dell'esposizione, in frazione del capitale dichiarato
    frazione_base: float = 0.15
    frazione_favorevole: float = 0.20
    frazione_piena: float = 0.25
    tetto: float = 0.25                 # limite duro, nessun gradino lo supera

    # condizioni di mercato per salire, sulla pendenza RELATIVA (vedi Segnale)
    pendenza_min_favorevole: float = 0.93
    pendenza_min_piena: float = 0.97
    vix_max_favorevole: float = 25.0
    vix_max_pieno: float = 20.0
    percentile_max: float = 60.0

    # condizioni di conto per salire
    giorni_min_favorevole: int = 20
    giorni_min_pieno: int = 40
    giorni_min_da_stop: int = 30
    risultato_min_eur: float = 0.0

    # ritirata: sopra questi valori l'esposizione va a zero
    vix_allarme: float = 30.0
    pendenza_ritirata: float = 0.85     # curva molto piu' piatta del solito

    # gestione
    stop_eur: float = 15.0
    pausa_giorni: int = 5
    banda_ribilancio: float = 0.40      # scarto tollerato prima di ritoccare
    stop_broker_max: float = 0.60       # distanza massima dello stop, in frazione


def conto_autorizzato(capital_env: str | None) -> bool:
    """Questa strategia esiste solo sul conto di prova.

    Il controllo e' qui, in una funzione pura, perche' e' la regola piu'
    importante del sistema e deve poter essere verificata da un test senza
    toccare rete, credenziali o broker.
    """
    return (capital_env or "").strip().lower() == "demo"


def scala(segnale: Segnale, conto: Conto, par: Parametri) -> Gradino:
    """Quanta esposizione tenere adesso, e per quali ragioni.

    Si parte dal gradino piu' alto e si scende al primo le cui condizioni sono
    tutte vere. La ritirata ha la precedenza su tutto: quando la curva si
    inverte il decadimento che paga questa strategia semplicemente non c'e'.
    """
    if conto.in_pausa:
        return Gradino("fermo", 0.0, ["il sistema e' in pausa dopo uno stop"])

    pend = segnale.pendenza_relativa
    if segnale.completo:
        if segnale.vix is not None and segnale.vix >= par.vix_allarme:
            return Gradino("ritirata", 0.0,
                           [f"VIX a {segnale.vix:.1f}, oltre la soglia di allarme "
                            f"{par.vix_allarme:.0f}"])
        if pend is not None and pend < par.pendenza_ritirata:
            return Gradino("ritirata", 0.0,
                           [f"la curva si sta appiattendo ({pend:.2f} rispetto al "
                            f"suo anno): il decadimento che paga questa strategia "
                            f"si sta spegnendo"])

    # Senza segnale non si sale e non si scende: si resta al gradino base.
    if not segnale.completo:
        return Gradino("base", min(par.frazione_base, par.tetto),
                       ["nessun dato sulla curva: resto al gradino base"])

    storico_ok, perche_storico = _storico_favorevole(conto, par)
    perc_ok = segnale.percentile is None or segnale.percentile <= par.percentile_max

    if (storico_ok
            and conto.giorni_operativi >= par.giorni_min_pieno
            and pend is not None and pend >= par.pendenza_min_piena
            and segnale.vix is not None and segnale.vix <= par.vix_max_pieno
            and perc_ok):
        return Gradino("pieno", min(par.frazione_piena, par.tetto), [
            f"curva ripida come nei periodi calmi ({pend:.2f} rispetto al suo anno)",
            f"VIX calmo a {segnale.vix:.1f}",
            f"{conto.giorni_operativi} giorni di operativita' alle spalle",
        ])

    if (storico_ok
            and conto.giorni_operativi >= par.giorni_min_favorevole
            and pend is not None and pend >= par.pendenza_min_favorevole
            and segnale.vix is not None and segnale.vix <= par.vix_max_favorevole
            and perc_ok):
        return Gradino("favorevole", min(par.frazione_favorevole, par.tetto), [
            f"curva nella norma ({pend:.2f} rispetto al suo anno)",
            f"VIX sotto {par.vix_max_favorevole:.0f}",
        ])

    motivi = [perche_storico] if not storico_ok else []
    if pend is not None and pend < par.pendenza_min_favorevole:
        motivi.append(f"curva piu' piatta del solito ({pend:.2f})")
    if segnale.vix is not None and segnale.vix > par.vix_max_favorevole:
        motivi.append(f"VIX a {segnale.vix:.1f}")
    if not perc_ok and segnale.percentile is not None:
        motivi.append(f"VIX al {segnale.percentile:.0f}° percentile della sua storia")
    return Gradino("base", min(par.frazione_base, par.tetto),
                   motivi or ["condizioni ordinarie"])


def _storico_favorevole(conto: Conto, par: Parametri) -> tuple[bool, str]:
    """Il conto ha il diritto di salire di gradino?"""
    if conto.giorni_da_ultimo_stop is not None and \
            conto.giorni_da_ultimo_stop < par.giorni_min_da_stop:
        return False, (f"solo {conto.giorni_da_ultimo_stop} giorni dall'ultimo stop, "
                       f"ne servono {par.giorni_min_da_stop}")
    if conto.risultato_cumulato_eur < par.risultato_min_eur:
        return False, (f"risultato cumulato {conto.risultato_cumulato_eur:+.2f}€ "
                       f"sotto la soglia per salire")
    if conto.giorni_operativi < par.giorni_min_favorevole:
        return False, (f"{conto.giorni_operativi} giorni di operativita', "
                       f"ne servono {par.giorni_min_favorevole}")
    return True, ""


def unita_target(nozionale_obiettivo: float, nozionale_unita: float) -> int:
    """Quante unita' minime servono per avvicinarsi all'esposizione voluta.

    Zero significa zero: quando il gradino e' la ritirata non si arrotonda a 1.
    """
    if nozionale_unita <= 0:
        return 0
    if nozionale_obiettivo <= 0:
        return 0
    return max(1, round(nozionale_obiettivo / nozionale_unita))


def serve_ribilancio(nozionale_attuale: float, nozionale_obiettivo: float,
                     size_attuale: float, par: Parametri) -> bool:
    """Vale la pena toccare la posizione, o lo scarto rientra nella banda?

    Ogni ritocco paga lo spread: sotto la banda si sta fermi. Fanno eccezione
    la prima apertura e l'ordine di uscire del tutto.
    """
    if nozionale_obiettivo <= 0:
        return abs(size_attuale) > 0
    if not size_attuale:
        return True
    return abs(nozionale_attuale / nozionale_obiettivo - 1) > par.banda_ribilancio


def livello_stop(prezzo: float, nozionale_target: float, par: Parametri) -> float | None:
    """Stop di mercato sul broker per una posizione corta.

    Il controllo giornaliero non protegge dai salti notturni: il 5 febbraio 2018
    questo strumento e' salito del 66% in una seduta. Il livello e' scelto perche'
    la perdita corrisponda al limite in euro, con un tetto alla distanza.
    """
    if prezzo <= 0 or nozionale_target <= 0:
        return None
    distanza = min(par.stop_broker_max, par.stop_eur / nozionale_target)
    return round(prezzo * (1 + distanza), 2)


def perdita_da_salto(nozionale: float, salto: float = 0.66) -> float:
    """Quanto costa, in euro, uno strappo come quello del 5 febbraio 2018.

    Serve a scrivere il numero nero su bianco ogni volta che l'esposizione sale:
    lo stop sul broker non protegge da un salto avvenuto a mercato chiuso.
    """
    return nozionale * salto


def stop_colpito(risultato_eur: float, size: float, par: Parametri) -> bool:
    """Il kill switch. Vale solo su una posizione davvero aperta."""
    if not size:
        return False
    return risultato_eur <= -par.stop_eur


def fine_pausa(adesso: datetime, par: Parametri) -> str:
    return (adesso + timedelta(days=par.pausa_giorni)).isoformat()


def in_pausa(stato: dict, adesso: datetime | None = None) -> bool:
    fino = stato.get("in_pausa_fino")
    if not fino:
        return False
    adesso = adesso or datetime.now(timezone.utc)
    return adesso.isoformat() < fino


def percentile(valore: float, storia: list[float]) -> float | None:
    """Dove sta `valore` dentro `storia`, in percentuale. None se non c'e' storia."""
    campione = [v for v in storia if v and v > 0]
    if len(campione) < 20:
        return None
    sotto = sum(1 for v in campione if v < valore)
    return 100.0 * sotto / len(campione)
