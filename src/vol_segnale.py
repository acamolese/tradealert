"""Legge dal broker lo stato della curva della volatilita'.

Capital quota VIX (l'indice) e VIXM (l'ETF sui futures a medio termine). Sono
grandezze diverse: un livello e un prezzo. Il loro rapporto non e' la pendenza
della curva e non ha soglie assolute sensate, perche' l'ETF scivola nel tempo
(mediana del rapporto: 1.79 nel 2021, 0.77 nel 2026, dati Capital settimanali).

Quello che si puo' misurare, e che qui si misura, e' se oggi quel rapporto e'
alto o basso rispetto al suo ultimo anno. Nei dati disponibili la separazione
c'e': nelle settimane di stress (VIX oltre 24, marzo 2026) il valore relativo
sta fra 0,74 e 0,87, nelle settimane calme fra 0,94 e 1,02.

VIX3M, la serie che userebbe la letteratura, non e' negoziabile su Capital: le
soglie tarate qui non sono trasferibili a quella serie.

Il modulo non decide niente: consegna un `Segnale`, e se il broker non risponde
consegna un segnale vuoto. Vuoto significa "non sappiamo", mai "via libera".
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.volatilita import Segnale, percentile

log = logging.getLogger(__name__)

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "vol"
EPIC_SPOT = "VIX"
EPIC_MEDIO = "VIXM"
SETTIMANE_STORIA = 104      # due anni di storia richiesti al broker
SETTIMANE_MEDIANA = 52      # finestra su cui si normalizza la pendenza
MINIMO_SETTIMANE = 20       # sotto questo la mediana non e' affidabile


def _prezzo_corrente(cap: Any, epic: str) -> float | None:
    try:
        sn = (cap.get_market(epic) or {}).get("snapshot") or {}
        bid, ask = float(sn.get("bid") or 0), float(sn.get("offer") or 0)
    except Exception as exc:
        log.warning("prezzo %s non disponibile: %s", epic, exc)
        return None
    if not bid or not ask:
        return None
    return (bid + ask) / 2


def _storia_settimanale(cap: Any, epic: str) -> dict[str, float]:
    """Chiusure settimanali per data, con cache giornaliera.

    La storia lunga cambia una volta a settimana e ogni chiamata a Capital pesa
    sul rate limit: si rilegge una volta al giorno.
    """
    oggi = datetime.now(timezone.utc).date().isoformat()
    f = CACHE / f"{epic}_{oggi}.json"
    if f.exists():
        try:
            salvato = json.loads(f.read_text())
            if isinstance(salvato, dict) and salvato:
                return salvato
        except Exception:
            pass   # cache illeggibile o in un formato vecchio: si rilegge
    try:
        grezze = cap.get_prices(epic, resolution="WEEK", max_bars=SETTIMANE_STORIA)
    except Exception as exc:
        log.warning("storia %s non disponibile: %s", epic, exc)
        return {}
    serie: dict[str, float] = {}
    for b in grezze or []:
        cp = b.get("closePrice") or {}
        giorno = (b.get("snapshotTimeUTC") or "")[:10]
        try:
            serie[giorno] = (float(cp["bid"]) + float(cp["ask"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
    if serie:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(serie))
        except OSError:
            pass
    return serie


def mediana_pendenza(spot: dict[str, float], medio: dict[str, float]) -> float | None:
    """Mediana del rapporto medio/spot sulle ultime settimane in comune.

    None quando le due serie non si sovrappongono abbastanza: senza questo
    riferimento il segnale non e' interpretabile e il sistema resta al gradino
    base invece di inventarsi una soglia.
    """
    comuni = sorted(set(spot) & set(medio))
    rapporti = [medio[d] / spot[d] for d in comuni[-SETTIMANE_MEDIANA:] if spot.get(d)]
    if len(rapporti) < MINIMO_SETTIMANE:
        log.warning("solo %d settimane in comune fra %s e %s: pendenza non "
                    "normalizzabile", len(rapporti), EPIC_SPOT, EPIC_MEDIO)
        return None
    return statistics.median(rapporti)


def leggi(cap: Any) -> Segnale:
    """Il segnale di adesso. Mai un'eccezione verso chi chiama."""
    vix = _prezzo_corrente(cap, EPIC_SPOT)
    vixm = _prezzo_corrente(cap, EPIC_MEDIO)
    storia_vix = _storia_settimanale(cap, EPIC_SPOT)
    storia_vixm = _storia_settimanale(cap, EPIC_MEDIO)

    s = Segnale(
        vix=vix,
        vixm=vixm,
        percentile=percentile(vix, list(storia_vix.values())) if vix else None,
        pendenza_mediana=mediana_pendenza(storia_vix, storia_vixm),
    )
    if not s.completo:
        log.warning("segnale volatilita' incompleto (VIX=%s VIXM=%s mediana=%s): "
                    "nessun aumento di esposizione autorizzato",
                    vix, vixm, s.pendenza_mediana)
    return s


def racconta(s: Segnale) -> str:
    """Una riga in italiano piano, per i messaggi e per i log."""
    if not s.completo:
        pezzi = []
        if s.vix:
            pezzi.append(f"VIX {s.vix:.1f}")
        pezzi.append("curva non confrontabile con la sua storia")
        return ", ".join(pezzi)
    rel = s.pendenza_relativa or 0
    if rel >= 0.97:
        verso = "ripida come nei periodi calmi"
    elif rel >= 0.93:
        verso = "nella norma"
    else:
        verso = "piu' piatta del solito (il mercato paga la protezione a breve)"
    pezzo = f"VIX {s.vix:.1f}, curva {verso} ({rel:.2f} rispetto al suo anno)"
    if s.percentile is not None:
        pezzo += f", VIX al {s.percentile:.0f}° percentile degli ultimi due anni"
    return pezzo
