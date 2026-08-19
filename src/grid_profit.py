"""Soglie di PROFITTO: avviso e blocco alla crescita del conto.

Richiesta 2026-08-19: "se il conto cresce di 10 euro un avviso, di 20 il blocco,
per capire cosa fare: capitalizzare o aumentare il budget".

E' il simmetrico del kill switch in perdita, ma con uno scopo diverso: non
protegge il capitale, obbliga a una DECISIONE quando il sistema ha prodotto
abbastanza da renderla sensata. Senza, un conto che cresce continua a girare alla
taglia iniziale e il guadagno non viene mai ne' incassato ne' reinvestito.

Lo stato e' per CONTO (non per profilo): la crescita e' del conto, e tutti i grid
che ci lavorano sopra devono fermarsi insieme. Logica pura, il job la orchestra.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdetto:
    stato: str            # "ok" | "avviso" | "blocco" | "gia_bloccato"
    guadagno: float
    baseline: float
    soglia_colpita: float | None
    messaggio: str


def valuta(
    equity: float,
    baseline: float,
    soglie_avviso: list[float],
    soglia_blocco: float,
    gia_avvisate: list[float],
    gia_bloccato: bool,
) -> Verdetto:
    """Confronta l'equity con la baseline e decide.

    soglie_avviso: livelli di guadagno che generano una notifica una sola volta.
    soglia_blocco: oltre questo guadagno si chiude tutto e non si riparte.
    """
    guadagno = equity - baseline

    if gia_bloccato:
        return Verdetto("gia_bloccato", guadagno, baseline, None,
                        "sistema in pausa per obiettivo raggiunto: serve una decisione")

    if soglia_blocco > 0 and guadagno >= soglia_blocco:
        return Verdetto("blocco", guadagno, baseline, soglia_blocco,
                        f"obiettivo +{soglia_blocco:.0f}€ raggiunto "
                        f"(guadagno {guadagno:+.2f}€): chiudo tutto e mi fermo")

    da_avvisare = [s for s in sorted(soglie_avviso)
                   if guadagno >= s and s not in gia_avvisate]
    if da_avvisare:
        s = da_avvisare[-1]
        return Verdetto("avviso", guadagno, baseline, s,
                        f"il conto e' cresciuto di {guadagno:+.2f}€ "
                        f"(soglia +{s:.0f}€)")

    return Verdetto("ok", guadagno, baseline, None, "")
