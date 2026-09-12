"""Carica i parametri della strategia di volatilita' da file, con override.

Ordine di precedenza, dal piu' debole al piu' forte:
  1. i default scritti in `src.volatilita.Parametri`
  2. `config/volatilita.json`, versionato
  3. le variabili d'ambiente `PAURA_*`, per intervenire sulla VM senza deploy

Il punto 3 esiste per retrocompatibilita': fino al 2026-09-12 era l'unico modo
di configurare il sistema, e la VM ha quelle variabili nel proprio ambiente.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import fields, replace
from pathlib import Path

from src.volatilita import Parametri

log = logging.getLogger(__name__)

FILE_DEFAULT = Path(__file__).resolve().parent.parent / "config" / "volatilita.json"

# Nome della variabile d'ambiente storica -> campo di Parametri.
ENV_LEGACY = {
    "PAURA_EPIC": "epic",
    "PAURA_ESPOSIZIONE": "frazione_base",
    "PAURA_TETTO": "tetto",
    "PAURA_STOP_EUR": "stop_eur",
    "PAURA_PAUSA_GG": "pausa_giorni",
    "PAURA_BANDA": "banda_ribilancio",
    "PAURA_VIX_ALLARME": "vix_allarme",
    "PAURA_FRAZIONE_PIENA": "frazione_piena",
}


def _converti(campo_tipo: type, grezzo: object) -> object:
    if campo_tipo is int:
        return int(float(grezzo))  # type: ignore[arg-type]
    if campo_tipo is float:
        return float(grezzo)       # type: ignore[arg-type]
    return str(grezzo)


def carica(percorso: Path | None = None, env: dict[str, str] | None = None) -> Parametri:
    """Parametri effettivi. Non solleva mai: un file rotto vale come assente."""
    par = Parametri()
    tipi = {f.name: f.type for f in fields(Parametri)}
    noti = set(tipi)

    percorso = percorso or FILE_DEFAULT
    dati: dict[str, object] = {}
    try:
        if percorso.exists():
            dati = json.loads(percorso.read_text())
    except Exception as exc:
        log.error("config volatilita illeggibile (%s): uso i default", exc)
        dati = {}

    cambi: dict[str, object] = {}
    for chiave, valore in dati.items():
        if chiave.startswith("_"):
            continue  # le chiavi con underscore sono commenti
        if chiave not in noti:
            log.warning("config volatilita: chiave sconosciuta '%s', ignorata", chiave)
            continue
        try:
            cambi[chiave] = _converti(_tipo(tipi[chiave]), valore)
        except (TypeError, ValueError):
            log.warning("config volatilita: valore non valido per '%s', ignorato", chiave)

    ambiente = os.environ if env is None else env
    for nome_env, campo in ENV_LEGACY.items():
        if nome_env not in ambiente:
            continue
        try:
            cambi[campo] = _converti(_tipo(tipi[campo]), ambiente[nome_env])
        except (TypeError, ValueError):
            log.warning("%s ha un valore non valido, ignorata", nome_env)

    par = replace(par, **cambi) if cambi else par
    return _coerente(par)


def _tipo(annotazione: object) -> type:
    """I campi sono annotati come stringhe (`from __future__ import annotations`)."""
    testo = annotazione if isinstance(annotazione, str) else getattr(
        annotazione, "__name__", "str")
    return {"int": int, "float": float, "str": str}.get(testo, str)


def _coerente(par: Parametri) -> Parametri:
    """Nessun gradino puo' superare il tetto, e i gradini devono salire.

    Il tetto e' l'unico numero che rappresenta il rischio massimo accettato:
    se qualcuno alza un gradino e si dimentica del tetto, vince il tetto.
    """
    base = min(par.frazione_base, par.tetto)
    favorevole = min(max(par.frazione_favorevole, base), par.tetto)
    piena = min(max(par.frazione_piena, favorevole), par.tetto)
    if (base, favorevole, piena) != (par.frazione_base, par.frazione_favorevole,
                                     par.frazione_piena):
        log.warning("scala dell'esposizione riportata dentro il tetto %.0f%%",
                    par.tetto * 100)
    return replace(par, frazione_base=base, frazione_favorevole=favorevole,
                   frazione_piena=piena)
