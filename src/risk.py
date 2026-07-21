"""Calcolo del sizing per trade su Capital.com con doppio vincolo:
budget di margine (EUR bloccati) + cap su perdita massima per trade.

Il margin factor effettivo dell'account NON e' ``instrument.marginFactor``
da ``/markets/{epic}`` (che resta a 100% statico per tutti gli strumenti):
deve essere derivato da ``/accounts/preferences.leverages`` come
``mf_eff = 1 / leverage[instrument_type]``. Vedi ``effective_margin_factor``.

Il sizing viene bloccato dal piu' restrittivo tra:
- size dal budget di margine: ``size_margin = (margin_budget / mf) / entry``
- size da max loss per trade: ``size_loss   = max_loss_eur / (entry * stop_pct/100)``

Se ``min_size`` del broker eccede entrambi i vincoli, il setup viene
scartato come ``non eseguibile entro risk cap``.

API:
    SizingResult.size           -> size finale (None se rifiutata)
    SizingResult.notional       -> esposizione generata (size * prezzo)
    SizingResult.margin_estimate-> margine davvero bloccato (EUR) ~ budget
    SizingResult.risk_estimate  -> perdita se scatta lo SL (EUR)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SizingResult:
    size: float | None
    notional: float
    margin_estimate: float
    risk_estimate: float
    reason: str = ""


def effective_margin_factor(
    market: dict[str, Any],
    leverages_map: dict[str, int] | None = None,
    epic_leverage: int | None = None,
) -> float:
    """Margin factor effettivo per questo strumento sull'account corrente.

    Prima fonte (se fornita): ``epic_leverage``, la leva REALE per-strumento
    (cap ESMA / auto-calibrata da ``position.leverage``, vedi ``src/leverage.py``).
    E' quella che Capital applica davvero e da cui dipende il margine bloccato;
    le preferences per-tipo la sovrastimano per gli strumenti a cap ridotto
    (Brent 10 vs COMMODITIES 20), facendo sforare il budget.
    Seconda fonte: leverage per il tipo instrument da ``/accounts/preferences``.
    Fallback: ``instrument.marginFactor`` da ``/markets/{epic}`` (statico 100%).
    """
    instrument = market.get("instrument", {}) or {}
    if epic_leverage and epic_leverage > 0:
        return 1.0 / float(epic_leverage)
    instr_type = instrument.get("type")
    if leverages_map and instr_type and leverages_map.get(instr_type):
        leverage = leverages_map[instr_type]
        if leverage > 0:
            return 1.0 / float(leverage)
    raw = instrument.get("marginFactor", 5) or 5
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return 0.05


# --- Conversione valuta quotata -> valuta di riferimento del sizing -----------
#
# Valuta di riferimento de-facto: USD. Tutti gli asset storici (Gold, Brent,
# US500, Nasdaq, Bitcoin) sono quote=USD e sono sempre stati trattati 1:1 col
# cap in EUR; tenere USD come riferimento lascia il loro sizing BIT-IDENTICO e
# mantiene R coerente con l'intera storia (baseline V1 -0.42R, gate, figure €).
# Solo i quote NON-USD (es. JPY, CHF) vanno convertiti, perche' li' il mismatch
# e' grossolano (USD/JPY: fattore ~175 -> trade sempre rifiutato). Vedi
# docs/sprint5-sizing-fix.md.
SIZING_REF_CCY = "USD"

# Mappa valuta quotata -> (epic da cui leggere il tasso vs USD, se invertire).
# "USD/{X}" (USD base): prezzo = X per 1 USD  -> 1 X = 1/prezzo USD (invert).
# "{X}/USD" (USD quote): prezzo = USD per 1 X  -> 1 X = prezzo USD (diretto).
_QUOTE_TO_USD_EPIC: dict[str, tuple[str, bool]] = {
    "JPY": ("USDJPY", True),
    "CHF": ("USDCHF", True),
    "HKD": ("USDHKD", True),  # Hang Seng (HK50) quotato in HKD (Sprint 5 trend block)
    "GBP": ("GBPUSD", False),
    "EUR": ("EURUSD", False),
    "AUD": ("AUDUSD", False),
}


def quote_to_ref_factor(quote_ccy: str | None, capital: Any) -> float | None:
    """Quanti USD (valuta di riferimento) vale 1 unita' della valuta quotata.

    Ritorna 1.0 se quote == USD (nessuna conversione, path bit-identico ai 5
    asset esistenti). Ritorna ``None`` se il tasso NON e' determinabile (valuta
    non mappata, fetch fallito, prezzo nullo): in quel caso il chiamante DEVE
    rifiutare il trade, mai ripiegare sul fattore 1 (che sarebbe il calcolo
    rotto). Meglio non aprire che aprire mal dimensionato.
    """
    if not quote_ccy or quote_ccy == SIZING_REF_CCY:
        return 1.0
    info = _QUOTE_TO_USD_EPIC.get(quote_ccy)
    if not info:
        return None
    epic, invert = info
    try:
        market = capital.get_market(epic) or {}
        snap = market.get("snapshot", {}) or {}
        bid, offer = snap.get("bid"), snap.get("offer")
        if not bid or not offer:
            return None
        rate = (float(bid) + float(offer)) / 2.0
        if rate <= 0:
            return None
        return (1.0 / rate) if invert else rate
    except Exception:
        return None


def _floor_to_step(value: float, step: float) -> float:
    """Arrotonda verso il basso al multiplo di ``step`` piu' vicino.
    Floor (non round-half) garantisce che il sizing non oltrepassi mai
    il vincolo da cui e' stato derivato (max loss o margine budget)."""
    if step <= 0:
        return value
    import math

    return math.floor(value / step) * step


def calculate_size(
    margin_budget: float,
    entry_price: float,
    margin_factor: float,
    min_size: float,
    size_step: float,
    stop_pct: float,
    available_margin: float | None = None,
    tolerance: float = 1.5,
    max_loss_per_trade_eur: float | None = None,
    quote_to_ref: float = 1.0,
) -> SizingResult:
    """``margin_budget``: EUR che vogliamo (al massimo) bloccare come
    margine su questo trade.

    ``max_loss_per_trade_eur``: se settato, la size finale viene cappata
    in modo che ``size * entry * stop_pct/100 <= max_loss``. Se anche
    ``min_size`` del broker comporta una perdita potenziale superiore al
    cap, il setup viene scartato.

    ``quote_to_ref``: fattore di conversione dalla valuta quotata alla valuta
    di riferimento del sizing (USD). Default 1.0 = nessuna conversione, path
    bit-identico al comportamento storico (tutti gli asset quote=USD). Per i
    quote non-USD (JPY, CHF) il chiamante passa il fattore reale; ``entry`` e
    le grandezze da esso derivate (rischio, notional, margine) vengono portate
    in valuta di riferimento prima del confronto coi cap. Vedi
    ``quote_to_ref_factor`` e docs/sprint5-sizing-fix.md.
    """
    if entry_price <= 0 or margin_factor <= 0:
        return SizingResult(
            None, 0, 0, 0, reason="Prezzo o margin factor non validi"
        )
    if stop_pct is None or stop_pct <= 0:
        return SizingResult(
            None, 0, 0, 0, reason="Stop loss % non valido"
        )

    # Prezzo equivalente in valuta di riferimento: con quote_to_ref=1.0 (USD)
    # coincide con entry_price -> sizing bit-identico agli asset esistenti.
    entry_ref = entry_price * quote_to_ref

    step = size_step or min_size
    target_notional = margin_budget / margin_factor
    raw_size_budget = target_notional / entry_ref

    risk_per_unit = entry_ref * stop_pct / 100.0
    if max_loss_per_trade_eur is not None and risk_per_unit > 0:
        raw_size_loss = max_loss_per_trade_eur / risk_per_unit
        raw_size = min(raw_size_budget, raw_size_loss)
    else:
        raw_size = raw_size_budget

    sized = _floor_to_step(raw_size, step)
    if sized < min_size:
        sized = min_size

    notional = sized * entry_ref
    margin_est = notional * margin_factor
    risk_est = sized * risk_per_unit

    # Caso 1: cap perdita massima per trade superato dalla size minima broker
    if max_loss_per_trade_eur is not None and risk_est > max_loss_per_trade_eur:
        return SizingResult(
            None,
            notional,
            margin_est,
            risk_est,
            reason=(
                f"Size minima {min_size} comporta perdita potenziale "
                f"{risk_est:.2f} EUR > cap {max_loss_per_trade_eur:.2f} EUR. "
                f"Setup non eseguibile entro risk cap."
            ),
        )

    # Caso 2: la size minima del broker blocca piu' margine del budget scelto
    if margin_est > margin_budget * tolerance:
        return SizingResult(
            None,
            notional,
            margin_est,
            risk_est,
            reason=(
                f"Size minima {min_size} blocca margine {margin_est:.2f} EUR > "
                f"{tolerance}x budget {margin_budget:.2f} EUR. "
                f"Asset non accessibile a questo budget."
            ),
        )

    # Caso 3: margine richiesto maggiore del margine disponibile sul conto
    if available_margin is not None and margin_est > available_margin:
        return SizingResult(
            None,
            notional,
            margin_est,
            risk_est,
            reason=(
                f"Margine richiesto {margin_est:.2f} > disponibile "
                f"{available_margin:.2f} sul conto"
            ),
        )

    return SizingResult(sized, notional, margin_est, risk_est)
