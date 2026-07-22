"""Reconcile: allinea lo stato dei trade nel DB con Capital.

Se una posizione e' chiusa su Capital (SL/TP colpito, chiusura manuale
dal frontend broker, close automatico) ma e' rimasta ``status='open'``
nel DB, il weekly report, l'analisi performance e il check
``max_open_positions`` leggono dati falsi.

Il reconcile:
1. Legge tutti i ``trades`` con ``status='open'`` dal DB.
2. Chiede a Capital le posizioni aperte in tempo reale.
3. Per ogni trade DB che non e' piu' sul broker, cerca la chiusura
   nella ``/history/activity`` (ultimi 30 giorni) e chiude il trade nel
   DB con close_price + pnl se disponibili, altrimenti lascia NULL.
"""

from __future__ import annotations

import logging
from typing import Any

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .db import Database
from .risk import quote_to_ref_factor
from .telegram_client import TelegramClient

log = logging.getLogger(__name__)


# Keyword nella description/reason dell'activity, fallback per tier
# che le popolano. La strategia primaria e' strutturale (vedi
# _find_close_activity): ``type=POSITION`` + ``status=ACCEPTED`` +
# direction opposta. Cosi' funziona per qualunque ragione di chiusura
# (SL hit, TP hit, manuale frontend broker, broker-forced) senza
# elenchi di ``source`` da mantenere.
_CLOSE_DESCRIPTIONS = {
    "POSITION_CLOSED",
    "CLOSED",
    "STOP_ORDER_FILLED",
    "PROFIT_ORDER_FILLED",
    "PARTIALLY_CLOSED",
}


def _opposite_dir(direction_word: str) -> str:
    return "SELL" if (direction_word or "").lower() == "long" else "BUY"


def _find_close_activity(
    activities: list[dict[str, Any]],
    deal_id: str,
    db_trade: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Dalla history cerca l'activity di chiusura per un deal_id.

    Strategia primaria, agnostica al ``source``: pattern strutturale
    ``type=POSITION`` + ``status=ACCEPTED`` + ``details.direction``
    opposta a quella del trade originale. Funziona per SL hit, TP hit,
    close manuale dal frontend broker, broker-forced (margin call,
    delisting), ecc., perche' tutti generano un counter-trade POSITION
    di direzione opposta. ``source`` resta un'informazione utile per
    derivare il tag (vedi ``_extract_close_info``).

    Strategia di fallback: ``description`` o ``details.reason`` con
    keyword nota. Mantenuta per tier che popolano questi campi.

    Sceglie il match piu' recente per dateUTC.
    """
    candidates: list[dict[str, Any]] = []
    opp_dir = _opposite_dir((db_trade or {}).get("direction", ""))

    for act in activities:
        if act.get("dealId") != deal_id:
            continue
        details = act.get("details") or {}
        # Strategia primaria: counter-trade strutturale.
        if (
            act.get("type") == "POSITION"
            and act.get("status") == "ACCEPTED"
            and details.get("direction") == opp_dir
        ):
            candidates.append(act)
            continue
        # Fallback keyword.
        desc = (act.get("description") or "").upper()
        if any(k in desc for k in _CLOSE_DESCRIPTIONS):
            candidates.append(act)
            continue
        reason = (
            details.get("reason") or details.get("actionType") or ""
        ).upper()
        if any(k in reason for k in _CLOSE_DESCRIPTIONS):
            candidates.append(act)

    if not candidates:
        return None
    candidates.sort(key=lambda a: a.get("dateUTC", ""), reverse=True)
    return candidates[0]


def _extract_close_info(
    activity: dict[str, Any],
    db_trade: dict[str, Any],
    quote_to_ref: float = 1.0,
) -> tuple[float | None, float | None, float | None, str]:
    """Ritorna (close_price, pnl, pnl_pct, exit_reason_tag).

    Sorgenti per close_price (in ordine): activity.level top-level
    (vecchio tier), details.level (nuovo tier counter-trade),
    details.closeLevel. Se pnl non e' fornito da Capital, lo deriva da
    (close-entry)*size con segno per direzione.

    ``quote_to_ref``: fattore valuta quotata -> riferimento (USD~EUR). Serve
    SOLO nel fallback ricostruito: (close-entry)*size e' nella valuta quotata
    dello strumento, che per Nikkei (JPY) o Hang Seng (HKD) NON e' EUR. Senza
    conversione il pnl finisce nel DB in yen/dollari-HK (es. Nikkei -1635 JPY
    invece di -8.85 EUR), gonfiando drawdown cap e report. Quando invece Capital
    fornisce ``amount``, quello e' gia' nella valuta del conto (EUR): NON si tocca.
    Tag e' derivato da ``source`` se presente (SL/TP/USER), altrimenti
    dalla description.
    """
    details = activity.get("details") or {}
    close_price = (
        activity.get("level")
        or details.get("level")
        or details.get("closeLevel")
    )
    pnl = details.get("amount") or activity.get("amount")
    direction = (db_trade.get("direction") or "").lower()
    entry = db_trade.get("entry_price")
    size = db_trade.get("size")

    if pnl is None and close_price and entry and size:
        delta = (float(close_price) - float(entry))
        if direction == "short":
            delta = -delta
        pnl = delta * float(size) * float(quote_to_ref or 1.0)
    pnl_pct = None
    if close_price and entry and direction:
        delta = (float(close_price) - float(entry)) / float(entry) * 100
        pnl_pct = -delta if direction == "short" else delta

    source = (activity.get("source") or "").upper()
    desc = (activity.get("description") or "").upper()
    if source == "SL" or "STOP" in desc:
        tag = "reconcile:stop_hit"
    elif source == "TP" or "PROFIT" in desc:
        tag = "reconcile:tp_hit"
    elif source == "USER":
        tag = "reconcile:closed_on_broker"
    elif "CLOSED" in desc:
        tag = "reconcile:closed_on_broker"
    else:
        tag = "reconcile:activity_match"

    return (
        float(close_price) if close_price is not None else None,
        float(pnl) if pnl is not None else None,
        float(pnl_pct) if pnl_pct is not None else None,
        tag,
    )


def _format_close_notification(
    trade: dict[str, Any],
    close_price: float | None,
    pnl: float | None,
    pnl_pct: float | None,
    tag: str,
) -> str:
    """Messaggio Telegram per chiusura rilevata dal reconcile. Diverso
    sui due rami: match (icona contestuale, dettagli pieni) vs no_match
    (warning, invito a verifica manuale). Senza questa notifica le
    chiusure notturne fuori dalla finestra monitor LLM (7-22) passavano
    in silenzio."""
    asset = trade.get("asset") or "?"
    entry = trade.get("entry_price")
    size = trade.get("size")
    entry_str = f"<code>{entry:g}</code>" if entry else "<code>n/d</code>"
    size_str = f"<code>{size:g}</code>" if size else "<code>n/d</code>"

    if tag == "reconcile:no_match":
        return (
            f"⚠️ <b>{asset} chiuso</b> "
            f"(motivo non identificato dal sistema)\n"
            f"Entry: {entry_str}  Size: {size_str}\n"
            f"<i>Capital ha chiuso la posizione ma il reconcile non ha"
            f" trovato l'evento corrispondente. Verifica manuale "
            f"consigliata su app Capital.</i>"
        )

    icon = "🛑" if "stop_hit" in tag else (
        "🎯" if "tp_hit" in tag else "🔚"
    )
    label = {
        "reconcile:stop_hit": "Stop loss colpito",
        "reconcile:tp_hit": "Take profit colpito",
        "reconcile:closed_on_broker": "Chiusura lato broker",
        "reconcile:activity_match": "Chiusura rilevata",
    }.get(tag, "Posizione chiusa")
    pnl_str = (
        f"<code>{pnl:+.2f}</code>" if pnl is not None else "<code>n/d</code>"
    )
    pnl_pct_str = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
    close_str = (
        f"<code>{close_price:g}</code>"
        if close_price is not None
        else "<code>n/d</code>"
    )
    return (
        f"{icon} <b>{label}</b>\n"
        f"Asset: <b>{asset}</b>  Size: {size_str}\n"
        f"Entry: {entry_str}  Close: {close_str}\n"
        f"P&amp;L: {pnl_str}{pnl_pct_str}"
    )


def reconcile_open_trades(
    config: Config,
    capital: CapitalClient | None = None,
    db: Database | None = None,
    live_positions: list[dict[str, Any]] | None = None,
    telegram: TelegramClient | None = None,
) -> dict[str, int]:
    """Allinea DB con broker. Ritorna contatori {checked, stale, closed,
    closed_with_pnl}. Loggare l'outcome e' compito del job entry point.

    Parametri opzionali per riuso da chiamanti che hanno gia' una sessione
    Capital aperta (es. position_monitor): se ``capital`` e' passato, non
    si fa un nuovo login; se ``live_positions`` e' passata, non si rifa
    la chiamata HTTP /positions. Se ``telegram`` e' passato, ad ogni
    chiusura riconciliata invia un messaggio (utile per intercettare le
    chiusure broker-side fuori finestra monitor LLM)."""
    if capital is None:
        capital = CapitalClient(config)
        capital.login()
    if db is None:
        db = Database(config)
    if telegram is None:
        telegram = TelegramClient(config)

    if live_positions is None:
        live_positions = capital.get_open_positions()
    live_deal_ids = {
        (p.get("position") or {}).get("dealId")
        for p in live_positions
    }
    live_deal_ids.discard(None)

    open_trades = db.get_open_trades()
    stale = [
        t for t in open_trades
        if t.get("capital_deal_id") and t["capital_deal_id"] not in live_deal_ids
    ]

    counters = {
        "checked": len(open_trades),
        "stale": len(stale),
        "closed": 0,
        "closed_with_pnl": 0,
    }

    if not stale:
        return counters

    # Fetch history: Capital demo limita lastPeriod a 86400s (24h) e
    # rifiuta range arbitrari con from/to. Con cron orario il reconcile
    # intercetta comunque ogni chiusura entro 24h: se un trade risulta
    # stale da piu' di un giorno viene chiuso nel DB senza close/pnl.
    try:
        activities = capital.get_activity_history(
            last_period_sec=24 * 3600,
            detailed=True,
        )
    except (CapitalAPIError, Exception) as exc:
        log.warning(
            "Fetch history/activity fallito (%s): chiudo senza close_price/pnl",
            exc,
        )
        activities = []

    for trade in stale:
        deal_id = trade["capital_deal_id"]
        activity = _find_close_activity(activities, deal_id, db_trade=trade)
        if activity:
            # Fattore valuta quotata -> EUR per il fallback ricostruito (solo se
            # Capital non fornisce l'amount reale). Best-effort: se non
            # determinabile resta 1.0 = comportamento storico.
            quote_to_ref = 1.0
            epic = activity.get("epic") or (activity.get("details") or {}).get("epic")
            if epic:
                try:
                    ccy = (
                        (capital.get_market(epic).get("instrument", {}) or {})
                        .get("currency")
                    )
                    q2r = quote_to_ref_factor(ccy, capital)
                    if q2r:
                        quote_to_ref = q2r
                    elif ccy and ccy != "USD":
                        log.warning(
                            "Reconcile %s (%s): conversione %s non determinabile, "
                            "pnl fallback resta in valuta quotata",
                            trade.get("asset"), deal_id, ccy,
                        )
                except Exception:
                    log.warning(
                        "Reconcile %s: fetch currency fallito, quote_to_ref=1.0",
                        trade.get("asset"),
                    )
            close_price, pnl, pnl_pct, tag = _extract_close_info(
                activity, trade, quote_to_ref=quote_to_ref
            )
            if pnl is not None:
                counters["closed_with_pnl"] += 1
        else:
            close_price, pnl, pnl_pct, tag = None, None, None, "reconcile:no_match"
        try:
            db.close_trade(
                deal_id=deal_id,
                close_price=close_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=tag,
            )
            counters["closed"] += 1
            log.info(
                "Reconcile chiuso: %s (%s) close=%s pnl=%s tag=%s",
                trade["asset"],
                deal_id,
                close_price,
                pnl,
                tag,
            )
            try:
                telegram.send_message(
                    _format_close_notification(
                        trade, close_price, pnl, pnl_pct, tag
                    )
                )
            except Exception:
                log.exception(
                    "Notifica Telegram chiusura reconcile fallita per %s",
                    deal_id,
                )
        except Exception:
            log.exception(
                "close_trade fallito per %s (%s)", trade["asset"], deal_id
            )
    return counters
