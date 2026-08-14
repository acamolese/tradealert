"""Configurazione TradeSpinner (spec §2). Le tabelle di premi/parametri sono
COSTANTI nel codice, versionate, NON calibrabili: modificarle richiede un record
constants_log con revisione avversaria (§6.2). RF_REF e' l'eccezione (dato di
mercato). I soli valori da env sono gli operativi (EXECUTION_TARGET, tolleranze demo).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- §2.1 premi per classe (excess return annuo atteso sul nozionale) ---
PREMIUM: dict[str, float] = {
    "equity_index": 0.045,
    "equity_etf": 0.045,
    "equity_stock": 0.045,
    "gold": 0.005,
    "fx": 0.000,
    "commodity": 0.000,
    "crypto": 0.000,
}

# --- §2.2 parametri (costanti dichiarate) ---
RF_REF = 0.02                 # tasso di riferimento annuo (aggiornato a mano, con log)
G_MIN = 0.02                  # soglia di apertura: g >= 2%/anno
G_EXIT = 0.0                  # sotto zero si chiude
F_MAX_POS = 1.0               # leva max per posizione
F_MAX_ACCOUNT = 1.5           # leva max aggregata
MAX_POSITIONS = 3
# Budget di leva per posizione nella COSTRUZIONE del portafoglio (constants_log
# 2026-07-30): senza budget ogni posizione entrava a f~F_MAX_POS e il tetto
# aggregato ne conteneva UNA sola (MAX_POSITIONS irraggiungibile per costruzione).
# La misura sul tabellone (f_exec/g_exec alla taglia massima) resta invariata.
# Eccezione ticket lumpy: 1 unita' anche sopra il budget, se sta in F_MAX_POS
# e nel budget aggregato residuo. G_MIN si valuta alla taglia aperta davvero.
F_BUDGET_POS = F_MAX_ACCOUNT / MAX_POSITIONS   # 0.5
MAX_PER_CLASS = 1
HOLDING_DAYS_MIN = 60         # orizzonte per ammortizzare lo spread
ENTRY_CONFIRM_SCANS = 2       # scansioni consecutive per entrare
EWMA_LAMBDA = 0.94            # riusa src/volatility.py
SPREAD_SAMPLES_MIN = 5
EMPTY_SET_KILL_WEEKS = 8


def mu_total(asset_class: str) -> float:
    """§2.1: MU_TOTAL = RF_REF + PREMIUM, tranne le classi senza deriva dichiarata
    (fx/commodity/crypto/bond) dove e' 0: il prezzo non ha premio, il carry sta
    tutto nel financing.

    'bond' e' entrato con il fix di classificazione 2026-08-14: un CFD su ETF
    obbligazionario NON incassa le cedole, quindi il rendimento atteso del PREZZO
    e' ~0, non il premio azionario. Non e' un premio nuovo: e' la rimozione di un
    premio attribuito per errore di tipizzazione (Capital marca gli ETF SHARES)."""
    if asset_class in ("fx", "commodity", "crypto", "bond"):
        return 0.0
    return RF_REF + PREMIUM.get(asset_class, 0.0)


# --- override di classificazione (fix 2026-08-14) ---
# instrumentType di Capital e' grossolano: 'SHARES' include tutti gli ETF (anche
# obbligazionari e monetari), 'INDICES' include indici valutari. Senza override il
# motore attribuisce il premio azionario a T-bill e al dollaro, e siccome g cresce
# al calare di sigma (f_opt = net_adj/sigma^2), l'errore finisce sistematicamente
# in CIMA alla classifica. Riconoscimento sul nome dello strumento, esplicito.
_BOND_MARKERS = (
    "TREASURY", "BOND", " MBS", "MBS ", "AGGREGATE", "GILT", "BUND", "BTP",
    "CORPORATE", "MUNICIPAL", "HIGH YIELD", "TIPS", "T-BILL", "FIXED INCOME",
)
_FX_INDEX_MARKERS = ("DOLLAR INDEX",)


# --- mappatura instrumentType Capital -> classe (§3 anagrafica) ---
def classify(instrument: dict) -> str:
    """Classe di asset da instrument. 'excluded' se non mappabile (mai eligible)."""
    t = (instrument.get("instrumentType") or instrument.get("type") or "").upper()
    epic = (instrument.get("epic") or "").upper()
    name = (instrument.get("name") or "").upper()
    # override sul nome PRIMA del tipo API (fix 2026-08-14): un ETF obbligazionario
    # resta obbligazionario anche se Capital lo marca SHARES.
    if any(m in name for m in _BOND_MARKERS):
        return "bond"
    if any(m in name for m in _FX_INDEX_MARKERS):
        return "fx"
    if t in ("CURRENCIES", "CURRENCY"):
        return "fx"
    if t in ("CRYPTOCURRENCIES", "CRYPTOCURRENCY"):
        return "crypto"
    if t == "INDICES":
        return "equity_index"
    if t == "SHARES":
        return "equity_stock"
    if t in ("ETF", "ETFS"):
        return "equity_etf"
    if t == "COMMODITIES":
        # l'oro ha un premio proprio (§2.1), le altre commodity no
        if "GOLD" in epic or "XAU" in epic or "GOLD" in name:
            return "gold"
        return "commodity"
    return "excluded"


@dataclass(frozen=True)
class SpinnerConfig:
    execution_target: str          # none|demo|real (§11)
    demo_fin_tolerance: float      # scarto rel. max financing modellato/addebitato
    demo_min_nights: int           # notti richieste per la promozione
    equity_cap: float | None = None  # tetto all'equity di lavoro (demo: rispecchia
                                     # il reale invece dei 1000 EURd del demo)
    telegram_prefix: str = "[ODDS]"
    # costanti riesposte (restano non-calibrabili)
    g_min: float = G_MIN
    f_max_pos: float = F_MAX_POS
    f_max_account: float = F_MAX_ACCOUNT
    max_positions: int = MAX_POSITIONS
    max_per_class: int = MAX_PER_CLASS
    holding_days_min: int = HOLDING_DAYS_MIN
    entry_confirm_scans: int = ENTRY_CONFIRM_SCANS
    spread_samples_min: int = SPREAD_SAMPLES_MIN
    empty_set_kill_weeks: int = EMPTY_SET_KILL_WEEKS


def load_spinner_config() -> SpinnerConfig:
    cap = os.environ.get("SPINNER_EQUITY_CAP")
    return SpinnerConfig(
        execution_target=os.environ.get("EXECUTION_TARGET", "none").strip().lower(),
        demo_fin_tolerance=float(os.environ.get("DEMO_FIN_TOLERANCE", "0.10")),
        demo_min_nights=int(os.environ.get("DEMO_MIN_NIGHTS", "10")),
        equity_cap=float(cap) if cap else None,
    )
