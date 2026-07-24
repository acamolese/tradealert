"""Leva reale per-strumento, usata dal sizing per stimare il margine.

Perche' serve: Capital NON espone la leva reale prima dell'apertura. Le
preferenze account (`/accounts/preferences`, `get_leverages_map`) danno la leva
per CATEGORIA (es. COMMODITIES 20, INDICES 20), ma i singoli strumenti hanno cap
regolatori piu' bassi (ESMA): il Brent (commodity non-oro) ha leva reale 10, non
20 - verificato dal campo `position.leverage` sul conto operativo. Il margine
reale bloccato = notional / leva_reale, quindi con la leva di categoria il sizing
sottostima il margine ~2x e i setup sforano il budget (docs/sizing-margin-bug).

Fonti, in ordine di priorita':
  1. override auto-calibrati: la leva reale letta da `position.leverage` dopo
     ogni apertura, persistita in logs/instrument_leverage.json. Si auto-corregge
     se un valore ESMA e' errato o se Capital cambia i cap.
  2. mappa ESMA qui sotto (cap retail EU standard).
  3. None -> il chiamante ripiega sulla leva di categoria (comportamento storico).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Cap ESMA per epic (leva max retail EU). OIL_BRENT confermato empiricamente = 10.
# In caso di dubbio si sceglie il valore piu' basso (sotto-dimensiona, mai sfora);
# l'auto-calibrazione alza al valore reale al primo trade dell'asset.
ESMA_LEVERAGE_BY_EPIC: dict[str, int] = {
    "OIL_BRENT": 10,  # commodity non-oro (confermato: position.leverage=10)
    "COPPER": 10,     # commodity non-oro
    "HK50": 10,       # indice non-maggiore (Hang Seng), coerente con ~70 EUR osservati
    "J225": 20,       # indice maggiore (Nikkei 225)
    "GOLD": 20,       # oro
    "US500": 20,      # indice maggiore
    "US100": 20,      # indice maggiore (Nasdaq 100)
    "DE40": 20,       # indice maggiore (DAX) - Sprint 8 ridisegno indici
    "US30": 20,       # indice maggiore (Dow) - Sprint 8 ridisegno indici
    "EURUSD": 30,     # FX maggiore
    "AUDUSD": 30,
    "GBPUSD": 30,
    "BTCUSD": 2,      # crypto
}

_OVERRIDE_PATH = (
    Path(__file__).resolve().parent.parent / "logs" / "instrument_leverage.json"
)


def _load_overrides() -> dict[str, int]:
    try:
        raw = json.loads(_OVERRIDE_PATH.read_text())
        return {k: int(v) for k, v in raw.items() if int(v) > 0}
    except Exception:
        return {}


def real_leverage(epic: str | None) -> int | None:
    """Leva reale per epic: override auto-calibrato > ESMA > None."""
    if not epic:
        return None
    ov = _load_overrides()
    if epic in ov:
        return ov[epic]
    return ESMA_LEVERAGE_BY_EPIC.get(epic)


def record_real_leverage(epic: str | None, leverage: float | int | None) -> None:
    """Registra la leva reale osservata (da `position.leverage` dopo l'apertura).
    Aggiorna l'override e logga un warning se diverge dal valore finora in uso.
    Best-effort: qualsiasi errore di I/O non deve toccare il flusso di apertura."""
    if not epic or not leverage or float(leverage) <= 0:
        return
    lev = int(round(float(leverage)))
    ov = _load_overrides()
    in_use = ov.get(epic) or ESMA_LEVERAGE_BY_EPIC.get(epic)
    if in_use == lev:
        return
    ov[epic] = lev
    try:
        _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OVERRIDE_PATH.write_text(json.dumps(ov, indent=2, sort_keys=True))
        log.warning(
            "Auto-calibrazione leva %s: %s -> %s (sizing usera' il valore reale)",
            epic, in_use, lev,
        )
    except Exception:
        log.exception("record_real_leverage: scrittura override fallita")
