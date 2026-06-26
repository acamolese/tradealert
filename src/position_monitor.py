"""Monitor sistematico delle posizioni aperte.

Passi per ogni posizione aperta:
1. ``_apply_trailing_stop``: sposta lo SL server-side se il profit in
   multipli R e' avanzato abbastanza (breakeven a 1R, +1R a 2R, ecc.).
2. ``_evaluate_position``: il LLM decide HOLD o CLOSE su thesis +
   feature aggiornate. Su CLOSE invia messaggio con bottoni (mclose/mhold).

I bottoni sono gestiti in modo asincrono dal listener daemon (vedi
``src.confirm_handler``), quindi qui niente polling inline.

Schedulato ogni 30 min in orario mercato.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

from anthropic import Anthropic

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .db import Database
from .features import compute_features
from .llm_usage import log_usage
from .quiet_hours import is_quiet_now, quiet_reason
from .telegram_client import TelegramClient
from .universe import ALL_KNOWN, UNIVERSE

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Sei il risk manager di un bot di swing trading retail.
Per ogni posizione aperta ricevi: dettagli del trade (asset, direzione,
entry, stop, take profit, P&L corrente, thesis originale del setup),
feature tecniche aggiornate (RSI, ATR, trend, distanza da massimi/minimi).

Decidi UNA azione tra:
- HOLD: la posizione va come previsto, niente da fare
- CLOSE: chiudi anticipatamente. Motivi validi: thesis invalidata,
  momentum invertito contro la posizione, livello tecnico chiave violato
  contro il trade, R:R residuo peggiorato sotto 1:1

Sii conservativo. Default HOLD nel dubbio. Proponi CLOSE solo se hai
segnali tecnici CHIARI di invalidazione. Non sovra-reagire al rumore di
qualche candela.

Rispondi SOLO con JSON valido, niente testo prima o dopo:
{
  "action": "HOLD" | "CLOSE",
  "reason": "spiegazione breve in italiano (max 2 righe)",
  "urgency": "low" | "medium" | "high"
}"""


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_epic(asset_name: str) -> str | None:
    for asset in ALL_KNOWN:
        if asset.name == asset_name:
            return asset.epic
    return None


def _resolve_tick_size(market: dict[str, Any]) -> float:
    """Ricava il tick size effettivo dello strumento.

    Preferenza: ``dealingRules.minStepDistance`` se in unita' POINTS
    (delta minimo accettato dal broker per stop/limit). Fallback:
    ``snapshot.decimalPlacesFactor`` come ``1 / 10**n``. Default 0.01.
    """
    rules = market.get("dealingRules") or {}
    msd = rules.get("minStepDistance") or {}
    if (msd.get("unit") == "POINTS") and msd.get("value"):
        try:
            v = float(msd["value"])
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    snap = market.get("snapshot") or {}
    dpf = snap.get("decimalPlacesFactor")
    try:
        if dpf is not None and int(dpf) >= 0:
            return 10.0 ** (-int(dpf))
    except (TypeError, ValueError):
        pass
    return 0.01


def _quantize_to_tick(value: float, tick: float) -> float:
    """Arrotonda ``value`` al multiplo piu' vicino di ``tick``.

    Il broker accetta solo valori multipli del tick, quindi qualunque
    valore intermedio viene riarrotondato lato server. Quantizzando in
    locale evitiamo che il confronto float new_sl vs current_sl
    (letto post-arrotondamento broker) generi loop sub-tick.
    """
    if tick <= 0:
        return value
    n_decimals = max(0, -int(round(math.log10(tick)))) if tick < 1 else 0
    return round(round(value / tick) * tick, n_decimals + 2)


# --- Trailing opzione D (Sprint 3) ----------------------------------------
# Ibrido: granularita' R a 0.25 (opzione A) + lock TP-aware "tardivo" che si
# attiva solo da >=80% del cammino entry->TP. Cosi' si protegge il profit in
# prossimita' del target (casi #45/#51/#55 dello Sprint 2) senza tagliare i
# mid-runner che ritracciano dal 50-70%. Razionale e simulazione in
# docs/sprint3-trailing-design.md.
#
# SOGLIE PRELIMINARI (80/90% e lock 0.45/0.65*rr): da calibrare dopo 5-10
# trade Sprint 3 con dati intra_trade_extreme reali. NON modificare nei primi
# trade per non contaminare la valutazione del fix.
_TRAIL_A_STEP_R = 0.25
_TRAIL_TP_LOCK_THRESHOLDS = ((0.90, 0.65), (0.80, 0.45))  # (frac_min, lock_frac)


def _trail_offset_granular(profit_r: float, step: float = _TRAIL_A_STEP_R) -> float:
    """Opzione A: half-risk granulare tra 0.5R e 1R, poi step sopra il BE.
    0.5R->-0.5, 0.75R->-0.25, 1R->0 (BE), 1.25R->+0.25, 1.5R->+0.5, ..."""
    if profit_r < 1.0:
        return -0.5 + math.floor((profit_r - 0.5) / step) * step
    return math.floor((profit_r - 1.0) / step) * step


def _trail_offset_v1_lowband(profit_r: float) -> float:
    """Rampa V1 'fascia bassa pulita' SOLO per 0.5<=profit_r<1.0 (deploy gated).
    Anticipa il lock rispetto a D nella fascia 0.5-1.0R, e a 1.0R riaggancia
    esattamente il breakeven di D (delta-trend zero per costruzione):
    0.5R->-0.25, 0.75R->-0.10, 1.0R->BE (=D). Vedi
    docs/sprint4-trailing-v1-clean.md. Definita solo nella fascia bassa: i
    chiamanti la usano solo se 0.5<=profit_r<1.0, fuori resta la curva D."""
    if profit_r < 0.75:
        return -0.25 + (profit_r - 0.5) / 0.25 * 0.15  # -0.25 -> -0.10
    return -0.10 + (profit_r - 0.75) / 0.25 * 0.10      # -0.10 -> 0.0 (BE)


def _trail_offset_v2_highband(profit_r: float) -> float:
    """Rampa V2 SOLO per 1.0<=profit_r<1.25 (deploy gated). Colma il buco di D
    che, in questa fascia, tiene lo SL a breakeven fino a 1.25R lasciando
    restituire il picco (casi #76, #79: Brent short picco ~1.1-1.24R -> BE ->
    give-back). V2 anticipa il lock salendo da BE a +0.25R:
    1.0R->0.0 (=D), 1.25R->+0.25 (=D). A 1.25R riaggancia D. Definita solo
    nella fascia 1.0-1.25R; fuori resta la curva D. Vedi
    docs/sprint5-trailing-v2-highband.md."""
    return 0.0 + (profit_r - 1.0) / 0.25 * 0.25  # 0.0 -> +0.25


def _trail_offset_tp_lock(frac_tp: float | None, rr: float | None) -> float | None:
    """Lock TP-aware tardivo, in unita' di R. None se sotto la soglia di
    attivazione o se TP/rr non disponibili."""
    if frac_tp is None or rr is None:
        return None
    for frac_min, lock_frac in _TRAIL_TP_LOCK_THRESHOLDS:
        if frac_tp >= frac_min:
            return lock_frac * rr
    return None


def _trailing_offset_r(profit_r: float, frac_tp: float | None,
                       rr: float | None, v1_lowband: bool = False,
                       v2_highband: bool = False) -> float:
    """Offset SL in unita' di R per l'opzione D: il piu' protettivo tra la
    granularita' A e il lock TP-aware tardivo.

    ``v1_lowband`` (deploy gated, default OFF = bit-identico a D): se True,
    nella SOLA fascia 0.5<=profit_r<1.0 usa la rampa V1 al posto della
    granularita' A. Fuori da quella fascia (incluso >=1.0R) e il lock TP-aware
    restano byte-identici a D, quindi a 1.0R V1 riaggancia il breakeven di D e
    il delta-trend resta zero. Vedi docs/sprint4-trailing-v1-clean.md.

    ``v2_highband`` (deploy gated, default OFF = bit-identico a D): se True,
    nella SOLA fascia 1.0<=profit_r<1.25 usa la rampa V2 (BE->+0.25R) al posto
    del piatto a BE di D, per proteggere il give-back del picco (casi #76/#79).
    A 1.0R e a 1.25R coincide con D (rampa agganciata): nessun salto. V1 e V2
    agiscono su fasce DISGIUNTE -> il gate fascia-bassa di V1 (peak 0.5-1.0R)
    non e' toccato da V2. Vedi docs/sprint5-trailing-v2-highband.md."""
    if v1_lowband and 0.5 <= profit_r < 1.0:
        off = _trail_offset_v1_lowband(profit_r)
    elif v2_highband and 1.0 <= profit_r < 1.25:
        off = _trail_offset_v2_highband(profit_r)
    else:
        off = _trail_offset_granular(profit_r)
    tp_off = _trail_offset_tp_lock(frac_tp, rr)
    if tp_off is not None and tp_off > off:
        off = tp_off
    return off


def _apply_trailing_stop(
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    position: dict[str, Any],
    step_r: float = 0.5,
    v1_lowband: bool = False,
    v2_highband: bool = False,
) -> None:
    """Trailing stop opzione D (Sprint 3), vedi docs/sprint3-trailing-design.md.
    L'offset dello SL in unita' di R e' il piu' protettivo tra:
    - granularita' A a 0.25R: 0.5R->-0.5, 0.75R->-0.25, 1R->BE, 1.25R->+0.25,
      1.5R->+0.5, ... (riempie il buco 1.0-1.5R del vecchio trailing);
    - lock TP-aware tardivo: da >=80% del cammino entry->TP blocca 0.45*rr,
      da >=90% blocca 0.65*rr (protegge la prossimita' al target).
    Solo migliorativo: se il nuovo SL e' peggiore dell'attuale non tocca.
    R e' derivato dallo stop originale del signal o dal trade orphan; il TP
    dal broker (``profitLevel``) o, in fallback, dal trade. ``step_r`` e'
    legacy (vecchia policy) e non e' piu' usato dalla D.
    """
    pos = position.get("position", {}) or {}
    market = position.get("market", {}) or {}
    deal_id = pos.get("dealId")
    direction = pos.get("direction")  # "BUY" | "SELL"
    entry = pos.get("level")
    current_sl = pos.get("stopLevel")
    epic = market.get("epic")  # epic sta in market, non in position
    asset_name = market.get("instrumentName") or epic or "?"

    if not (deal_id and direction and entry and current_sl):
        return

    trade = db.get_trade_by_deal_id(deal_id)

    # Posizione aperta fuori-bot: la importiamo nel DB. Due casi:
    #
    # 1. RECOVERY (bug #6): esiste un signal recente con status
    #    'execute_inconsistent' compatibile per epic+direction. Significa
    #    che il bot l'aveva aperta ma il persist su Supabase e' fallito.
    #    Ricostruiamo il link signal_id valido cosi' il sample resta
    #    valido per la validazione hit-rate.
    #
    # 2. ORPHAN classico: nessun signal candidato -> apertura manuale
    #    dell'utente lato Capital, signal_id=None.
    #
    # In entrambi i casi usiamo lo SL attuale come riferimento R-distance.
    if not trade:
        if not current_sl:
            log.warning(
                "Trailing SKIP: posizione manuale %s (%s) senza SL, "
                "impossibile derivare R-distance",
                deal_id,
                asset_name,
            )
            return
        size = pos.get("size") or 0
        direction_norm = "long" if direction == "BUY" else "short"
        profit_level = pos.get("profitLevel")

        # Recovery a due livelli:
        #
        # Tier 2 (fast path, deterministico): cerca in signal_to_trade_link
        # un record con questo capital_deal_id. Se l'executor era arrivato
        # ad aprire la posizione su Capital prima di crashare,
        # link_attempt_capital_open ha gia' salvato (signal_id, deal_id)
        # esatti. Niente euristica, match diretto.
        #
        # Tier 1 (fallback euristico): se il fast path non trova nulla
        # (es. eccezione PRIMA di link_attempt_capital_open, o link
        # writes falliti), si tenta il match per epic+direction+window
        # su signal execute_inconsistent.
        recovered_signal: dict[str, Any] | None = None
        recovery_tier: str | None = None
        ambiguous_match = False

        try:
            link = db.find_link_by_deal_id(deal_id)
        except Exception:
            log.exception(
                "find_link_by_deal_id fallito per %s", deal_id
            )
            link = None

        if link and link.get("status") in ("capital_open", "attempting"):
            signal_id_from_link = link.get("signal_id")
            if signal_id_from_link:
                try:
                    sig = db.get_signal(signal_id_from_link)
                    if sig:
                        recovered_signal = sig
                        recovery_tier = "tier2_link"
                except Exception:
                    log.exception(
                        "get_signal %s fallito durante recovery tier 2",
                        signal_id_from_link,
                    )

        if not recovered_signal and epic:
            try:
                candidates = db.find_inconsistent_signal(
                    epic=epic,
                    direction=direction_norm,
                    window_minutes=10,
                )
                if len(candidates) == 1:
                    recovered_signal = candidates[0]
                    recovery_tier = "tier1_inconsistent"
                elif len(candidates) > 1:
                    ambiguous_match = True
                    log.warning(
                        "Recovery ambiguo per %s (%s): %d signal "
                        "execute_inconsistent candidati, fallback orphan",
                        deal_id,
                        asset_name,
                        len(candidates),
                    )
            except Exception:
                log.exception(
                    "find_inconsistent_signal fallito per %s", deal_id
                )

        recovery_reason_map = {
            "tier2_link": "recovered:link_capital_open",
            "tier1_inconsistent": "recovered:execute_inconsistent",
        }
        trade_row = {
            "signal_id": (
                recovered_signal["id"] if recovered_signal else None
            ),
            "capital_deal_id": deal_id,
            "asset": asset_name,
            "direction": direction_norm,
            "size": float(size),
            "entry_price": float(entry),
            "current_sl": float(current_sl),
            "current_tp": (
                float(profit_level) if profit_level else None
            ),
            "status": "open",
            "exit_reason": (
                recovery_reason_map.get(
                    recovery_tier or "", "manual_import:trailing"
                )
            ),
        }

        try:
            trade = db.insert_trade(trade_row)
        except Exception:
            log.exception(
                "Import (recovery=%s) fallito per posizione %s",
                bool(recovered_signal),
                deal_id,
            )
            return

        if recovered_signal:
            # Update signal status + monitoring event + notifica.
            # Tutti gli errori sotto sono best-effort: il trade row
            # gia' creato e' la sorgente di verita'.
            from datetime import datetime, timezone

            sig_created = recovered_signal.get("created_at", "")
            latency_s: float | None = None
            try:
                created_dt = datetime.fromisoformat(
                    sig_created.replace("Z", "+00:00")
                )
                latency_s = (
                    datetime.now(timezone.utc) - created_dt
                ).total_seconds()
            except Exception:
                pass

            try:
                db.update_signal_status(
                    recovered_signal["id"], "executed_recovered"
                )
            except Exception:
                log.exception(
                    "update_signal_status executed_recovered fallito "
                    "per signal %s",
                    recovered_signal["id"],
                )

            # Chiusura della macchina a stati del link (best-effort).
            # Il link era in 'capital_open' o 'attempting': lo portiamo a
            # 'persisted' visto che adesso esiste la riga in trades.
            try:
                db.link_attempt_persisted(recovered_signal["id"])
            except Exception:
                log.exception(
                    "link_attempt_persisted fallito per signal %s "
                    "durante recovery",
                    recovered_signal["id"],
                )

            tier_label = recovery_tier or "?"
            try:
                db.insert_monitoring_event(
                    {
                        "trade_id": trade["id"],
                        "event_type": "orphan_adopted_recovered",
                        "reason": (
                            f"Recovery signal {recovered_signal['id']} "
                            f"({tier_label}) -> trade {trade['id']}"
                        ),
                        "details": {
                            "signal_id": recovered_signal["id"],
                            "signal_created_at": sig_created,
                            "latency_seconds": latency_s,
                            "deal_id": deal_id,
                            "epic": epic,
                            "direction": direction_norm,
                            "recovery_tier": tier_label,
                        },
                    }
                )
            except Exception:
                log.exception("Monitoring event recovered fallito")

            log.warning(
                "RECOVERY: posizione %s (%s) linkata a signal %d "
                "(tier=%s), latency=%.1fs",
                deal_id,
                asset_name,
                recovered_signal["id"],
                tier_label,
                latency_s or -1,
            )
            try:
                lat_txt = (
                    f" (latency {latency_s:.0f}s)"
                    if latency_s is not None
                    else ""
                )
                telegram.send_message(
                    f"♻️ <b>Recovery posizione</b> [{tier_label}]\n"
                    f"<b>{_esc(asset_name)}</b> ({direction_norm}) "
                    f"linkata a signal #{recovered_signal['id']}{lat_txt}"
                )
            except Exception:
                log.exception("Telegram recovery notice fallita")
        else:
            # Vero orphan: signal_id=None, niente signal da aggiornare.
            try:
                db.insert_monitoring_event(
                    {
                        "trade_id": trade["id"],
                        "event_type": "orphan_adopted",
                        "reason": (
                            "Posizione adottata senza signal candidato"
                            + (" (match ambiguo)" if ambiguous_match else "")
                        ),
                        "details": {
                            "deal_id": deal_id,
                            "epic": epic,
                            "direction": direction_norm,
                            "ambiguous": ambiguous_match,
                        },
                    }
                )
            except Exception:
                log.exception("Monitoring event orphan fallito")

            log.warning(
                "ORPHAN: posizione %s (%s) importata senza signal_id "
                "(ambiguous=%s)",
                deal_id,
                asset_name,
                ambiguous_match,
            )
            try:
                tag = "ambiguo" if ambiguous_match else "manuale"
                telegram.send_message(
                    f"⚠️ <b>Posizione orphan adottata</b> ({tag})\n"
                    f"<b>{_esc(asset_name)}</b> ({direction_norm}) "
                    f"senza signal collegato.\n"
                    f"Trailing attivo dal prossimo ciclo."
                )
            except Exception:
                log.exception("Telegram orphan notice fallita")

    # Deriva lo stop_pct: se c'e un signal originale usa il suo, altrimenti
    # lo ricava dallo SL iniziale salvato sul trade (stabile nel tempo).
    signal = (
        db.get_signal(trade["signal_id"])
        if trade.get("signal_id") is not None
        else None
    )
    if signal and signal.get("stop_loss"):
        stop_pct = float(signal["stop_loss"])
    elif trade.get("entry_price") and trade.get("current_sl"):
        ref_entry = float(trade["entry_price"])
        ref_sl = float(trade["current_sl"])
        stop_pct = abs(ref_entry - ref_sl) / ref_entry * 100 if ref_entry else 0.0
    else:
        log.warning(
            "Trailing SKIP: stop_pct non derivabile per %s (%s)",
            deal_id,
            asset_name,
        )
        return
    entry = float(entry)
    current_sl = float(current_sl)
    if stop_pct <= 0:
        return

    r_distance = entry * stop_pct / 100

    # Fetch market completo (dealingRules + snapshot freschi) per ricavare
    # tick size e bid/offer aggiornati. Capital arrotonda lo stopLevel al
    # tick (es. Brent: 0.001 -> 3 decimali). Senza quantizzare al tick il
    # confronto float new_sl vs current_sl genera trigger sub-tick a ogni
    # ciclo (loop osservato su trade Brent #19 il 2026-04-28: 25+ trigger
    # in 4h con delta 0.00025 = un quarto di tick).
    market_full: dict[str, Any] = {}
    if epic:
        try:
            market_full = capital.get_market(epic) or {}
        except Exception:
            market_full = {}
    snap_full = market_full.get("snapshot") or {}

    bid = market.get("bid") or snap_full.get("bid")
    offer = market.get("offer") or snap_full.get("offer")
    if bid is not None and offer is not None:
        current_price = (float(bid) + float(offer)) / 2
    else:
        current_price = None

    if current_price is None:
        return

    tick_size = _resolve_tick_size(market_full)

    if direction == "BUY":
        profit = current_price - entry
    else:
        profit = entry - current_price

    profit_r = profit / r_distance
    if profit_r < 0.5:
        return

    # Opzione D: offset = max protettivo tra granularita' A e lock TP-aware
    # tardivo. Il TP serve per la frazione di cammino entry->TP; se assente
    # (raro) la D degrada all'opzione A, comunque migliorativa.
    broker_tp_now = pos.get("profitLevel")
    if broker_tp_now:
        tp_price: float | None = float(broker_tp_now)
    elif trade and trade.get("current_tp"):
        tp_price = float(trade["current_tp"])
    else:
        tp_price = None
    if tp_price is not None and abs(tp_price - entry) > 0:
        rr = abs(tp_price - entry) / r_distance
        frac_tp: float | None = profit / abs(tp_price - entry)
    else:
        rr = None
        frac_tp = None
    offset_r = _trailing_offset_r(
        profit_r, frac_tp, rr, v1_lowband=v1_lowband, v2_highband=v2_highband
    )
    # Controfattuale D PURO (V1 e V2 OFF) loggato in parallelo: baseline per le
    # metriche forward. Resta puro D anche con V1/V2 ON, cosi' il gate
    # fascia-bassa di V1 (exit_R vs D) non e' toccato dall'aggiunta di V2.
    offset_r_d = _trailing_offset_r(
        profit_r, frac_tp, rr, v1_lowband=False, v2_highband=False
    )

    if direction == "BUY":
        new_sl_raw = entry + offset_r * r_distance
    else:
        new_sl_raw = entry - offset_r * r_distance

    # Quantizzazione al tick e soglia minima di mezzo tick: skip update se
    # la differenza e' sotto un tick intero. Confronto post-quantizzazione
    # cosi' siamo in linea con cio' che il broker accetta come stopLevel.
    new_sl = _quantize_to_tick(new_sl_raw, tick_size)
    half_tick = tick_size / 2 if tick_size > 0 else 1e-9
    if direction == "BUY":
        if new_sl <= current_sl + half_tick:
            return
    else:
        if new_sl >= current_sl - half_tick:
            return

    # Dedup notifica: se l'ultimo trailing_sl scritto su monitoring_events
    # ha lo stesso new_sl entro tick, evita di rinotificare. Layer di
    # difesa aggiuntivo oltre alla quantizzazione (copre eventuali drift
    # di lettura SL tra Capital e DB).
    last_event = db.get_last_monitoring_event(
        trade["id"], "trailing_sl"
    ) if trade else None
    last_new_sl = (
        (last_event.get("details") or {}).get("new_sl")
        if last_event
        else None
    )
    suppress_telegram = (
        last_new_sl is not None
        and abs(float(last_new_sl) - new_sl) < tick_size
    )
    # Capital PUT /positions/{dealId} sostituisce i level non passati con
    # null (rimuove il TP). Per preservare il take profit dobbiamo SEMPRE
    # ripassarlo nel body. Sorgente: lo state live del broker
    # (``pos.profitLevel``) e' la verita' attuale; il DB ``trade.current_tp``
    # serve solo come backup se il broker l'ha gia' perso (in tal caso non
    # lo ripristiniamo, per non modificare retroattivamente posizioni
    # legacy aperte prima di questo fix).
    broker_tp = pos.get("profitLevel")
    profit_level_to_pass = float(broker_tp) if broker_tp else None
    try:
        capital.update_position(
            deal_id,
            stop_level=new_sl,
            profit_level=profit_level_to_pass,
        )
    except Exception as exc:
        log.warning("Trailing SL fallito per %s: %s", asset_name, exc)
        return

    tp_log = (
        f"TP {profit_level_to_pass:g} preserved"
        if profit_level_to_pass is not None
        else "broker TP=None (no preserve)"
    )
    log.info(
        "Trailing SL %s: %s -> %s (profit %.2fR, offset %+0.2fR) | %s",
        asset_name,
        current_sl,
        new_sl,
        profit_r,
        offset_r,
        tp_log,
    )
    reason_text = (
        f"Profit {profit_r:.2f}R, SL {offset_r:+.2f}R dall'entry"
    )
    if trade:
        db.insert_monitoring_event(
            {
                "trade_id": trade["id"],
                "event_type": "trailing_sl",
                "reason": reason_text,
                "details": {
                    "old_sl": current_sl,
                    "new_sl": new_sl,
                    "current_price": current_price,
                    "r_distance": r_distance,
                    "profit_r": profit_r,
                    "offset_r": offset_r,
                    "offset_r_d": offset_r_d,
                    "trail_variant": (
                        "v1_lowband" if (v1_lowband and 0.5 <= profit_r < 1.0)
                        else "v2_highband" if (v2_highband and 1.0 <= profit_r < 1.25)
                        else "D"
                    ),
                },
            }
        )
    if offset_r < 0:
        # offset -0.5 -> 0.5R di rischio residuo, -0.25 -> 0.25R, ecc.
        # Con i gradini fini di D il rischio residuo non e' sempre "meta'".
        label = f"rischio residuo {-offset_r:g}R"
    elif offset_r == 0:
        label = "breakeven (rischio zero)"
    else:
        label = f"+{offset_r:g}R in profitto"
    if suppress_telegram:
        log.info(
            "Trailing notifica soppressa per %s: stesso SL entro tick "
            "rispetto a ultimo trailing_sl loggato",
            asset_name,
        )
    else:
        telegram.send_message(
            f"🛡 <b>Trailing SL</b> su {_esc(asset_name)}\n"
            f"Profit: <code>{profit_r:.2f}R</code> → SL {label}\n"
            f"SL: <code>{current_sl}</code> → <code>{new_sl}</code>"
        )


def _evaluate_position(
    config: Config,
    capital: CapitalClient,
    db: Database,
    position: dict[str, Any],
) -> dict[str, Any] | None:
    """Ritorna {action, reason, urgency, ...} o None se non valutabile."""
    pos = position.get("position", {}) or {}
    market = position.get("market", {}) or {}
    deal_id = pos.get("dealId")
    epic = market.get("epic")  # epic sta in market.epic, non in position.epic
    direction = pos.get("direction")
    entry = pos.get("level")
    size = pos.get("size")
    pnl = pos.get("upl") or pos.get("profitAndLoss") or pos.get("pnl")
    asset_name = market.get("instrumentName") or epic

    if not (epic and direction and entry):
        log.warning("Posizione %s senza dati sufficienti", deal_id)
        return None

    trade = db.get_trade_by_deal_id(deal_id) if deal_id else None
    thesis = ""
    if trade and trade.get("signal_id"):
        signal = db.get_signal(trade["signal_id"])
        if signal:
            thesis = signal.get("thesis", "")

    try:
        candles = capital.get_prices(epic, resolution="HOUR_4", max_bars=60)
        snapshot = capital.get_market(epic)
    except CapitalAPIError as exc:
        log.warning("Skip valutazione %s: %s", asset_name, exc)
        return None

    features = compute_features(asset_name, candles, snapshot=snapshot)
    current_price = features.get("last_price")
    current_pnl_pct = (
        ((current_price - entry) / entry * 100)
        if current_price and direction == "BUY"
        else ((entry - current_price) / entry * 100) if current_price else None
    )

    payload = {
        "position": {
            "asset": asset_name,
            "direction": direction,
            "entry_price": entry,
            "current_price": current_price,
            "stop_loss": pos.get("stopLevel"),
            "take_profit": pos.get("profitLevel"),
            "size": size,
            "pnl_currency": pnl,
            "pnl_pct_estimate": current_pnl_pct,
            "thesis_originale": thesis,
        },
        "current_features": features,
    }

    # Monitor task: HOLD/CLOSE su una singola posizione, payload piccolo
    # e output strutturato corto. Haiku basta e taglia ~80% del costo
    # rispetto a Sonnet sulle ~16 chiamate giornaliere in finestra attiva.
    client = Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.anthropic_model_fast,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
    )
    log_usage(config, "monitor", response)
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("LLM JSON parse fail: %s | raw=%r", exc, raw[:200])
        return None

    decision["deal_id"] = deal_id
    decision["asset"] = asset_name
    decision["current_price"] = current_price
    decision["pnl_pct"] = current_pnl_pct
    return decision


def _format_close_proposal(decision: dict[str, Any]) -> str:
    urgency_icon = {"high": "🚨", "medium": "⚠️", "low": "ℹ️"}.get(
        decision.get("urgency", "low"), "ℹ️"
    )
    pnl = decision.get("pnl_pct")
    pnl_str = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "-"
    return (
        f"{urgency_icon} <b>Proposta di chiusura</b>\n\n"
        f"<b>{_esc(decision['asset'])}</b>\n"
        f"P&amp;L attuale: <code>{pnl_str}</code>\n"
        f"Prezzo: <code>{decision.get('current_price')}</code>\n\n"
        f"<i>{_esc(decision.get('reason', ''))}</i>"
    )


def _resolve_close_price_from_activity(
    capital: CapitalClient,
    deal_id: str,
    db_trade: dict[str, Any],
    retries: int = 3,
    sleep_sec: float = 1.0,
) -> float | None:
    """Cerca nell'activity history il prezzo di esecuzione del counter-trade
    di chiusura. ``confirm_deal`` su Capital, per un close manuale di
    posizione, ritorna in ``level`` l'entry della posizione originale
    (non il prezzo del counter-trade).

    Riutilizza la helper ``_find_close_activity`` di ``reconcile.py``
    (logica unica per riconoscere counter-trade strutturali) e ne legge
    il ``close_price`` via ``_extract_close_info``. Cosi' i due path
    di chiusura DB (close_position_by_deal_id e reconcile) condividono
    lo stesso parser e qualunque fix futuro vale per entrambi.

    Capital ha lag di 1-2s tra DELETE /positions e visibilita'
    nell'activity: retry breve per assorbirlo.
    """
    import time
    from .reconcile import _find_close_activity, _extract_close_info

    for attempt in range(retries):
        try:
            activities = capital.get_activity_history(
                last_period_sec=600, detailed=True
            )
        except Exception:
            activities = []
        found = _find_close_activity(activities, deal_id, db_trade=db_trade)
        if found:
            close_price, _pnl, _pct, _tag = _extract_close_info(
                found, db_trade
            )
            if close_price:
                return close_price
        if attempt < retries - 1:
            time.sleep(sleep_sec)
    return None


def _resolve_close_price_from_market(
    capital: CapitalClient, epic: str | None, direction_word: str
) -> float | None:
    """Subordinato: se l'activity non risponde, usa il prezzo a cui il
    broker chiuderebbe ora (bid se chiudiamo un long, offer se chiudiamo
    uno short) come migliore approssimazione del fill effettivo."""
    if not epic:
        return None
    try:
        snap = (capital.get_market(epic) or {}).get("snapshot") or {}
    except Exception:
        return None
    if direction_word == "long":
        price = snap.get("bid")
    else:
        price = snap.get("offer")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def _auto_close_with_veto(
    config: Config,
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    deal_id: str,
    decision: dict[str, Any],
) -> None:
    """Auto-chiusura con finestra di veto (simmetrica all'auto-confirm
    apertura). Invia la proposta col solo bottone "Lascia aperta", poll del
    veto per ``auto_close_window_sec``; se l'utente clicca (il listener scrive
    un evento ``close_vetoed``) si annulla, altrimenti si chiude da soli.
    Best-effort: qualunque errore non deve lasciare la posizione in stato
    ambiguo -> in caso di eccezione NON chiude (fail-safe verso il non-agire)."""
    window = getattr(config, "auto_close_window_sec", 30) or 30
    trade = db.get_trade_by_deal_id(deal_id)
    trade_id = trade.get("id") if trade else None

    # marker pre-finestra: ultimo veto registrato per questo trade (per
    # rilevare un veto NUOVO arrivato durante la finestra).
    pre_veto = (
        db.get_last_monitoring_event(trade_id, "close_vetoed")
        if trade_id else None
    )
    pre_veto_id = pre_veto.get("id") if pre_veto else None

    buttons = [[{"text": "⏸ Lascia aperta", "callback_data": f"mhold:{deal_id}"}]]
    telegram.send_message_with_buttons(
        _format_close_proposal(decision)
        + f"\n\n⏳ <i>Auto-chiusura tra {window}s salvo veto.</i>",
        buttons,
    )

    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        time.sleep(min(5, window))
        if trade_id is None:
            continue
        latest = db.get_last_monitoring_event(trade_id, "close_vetoed")
        if latest and latest.get("id") != pre_veto_id:
            log.info("Auto-close VETATO da utente su %s (deal %s)",
                     decision.get("asset"), deal_id)
            telegram.send_message(
                f"⏸ <b>{_esc(decision.get('asset'))}</b>: auto-chiusura annullata (veto)."
            )
            return

    # nessun veto: chiude
    log.info("Auto-close: finestra scaduta, chiudo %s (deal %s)",
             decision.get("asset"), deal_id)
    close_position_by_deal_id(
        capital, db, telegram, deal_id,
        reason="auto-close LLM monitor",
    )


def close_position_by_deal_id(
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    deal_id: str,
    reason: str,
    message_id: int | None = None,
) -> None:
    """Chiude una posizione per deal_id e persiste l'evento. Riutilizzabile
    sia dal monitor (quando l'LLM propone CLOSE) sia dal daemon (click
    ``mclose:`` dell'utente)."""
    try:
        # Cattura entry e direzione PRIMA del close, per i fallback di
        # close_level e pnl quando confirm_deal non li popola correttamente.
        trade = db.get_trade_by_deal_id(deal_id)
        direction_word = (
            (trade.get("direction") or "").lower() if trade else ""
        )
        entry = float(trade.get("entry_price") or 0) if trade else 0.0
        size = float(trade.get("size") or 0) if trade else 0.0
        signal = (
            db.get_signal(trade["signal_id"])
            if trade and trade.get("signal_id")
            else None
        )
        epic = (signal or {}).get("epic")

        close_resp = capital.close_position(deal_id)
        deal_ref = close_resp.get("dealReference")
        confirm = capital.confirm_deal(deal_ref) if deal_ref else {}
        confirm_level = float(confirm.get("level") or 0)
        pnl_raw = confirm.get("profit") or confirm.get("profitAndLoss")
        pnl = float(pnl_raw) if pnl_raw is not None else None

        # confirm.level e' inaffidabile per close manuali (restituisce
        # l'entry originale). Sorgente primaria: activity history (stessa
        # helper usata da reconcile). Sorgente subordinata: bid/offer
        # current. Ultima risorsa: confirm_level.
        activity_level = _resolve_close_price_from_activity(
            capital, deal_id, trade or {}
        )
        market_level = (
            _resolve_close_price_from_market(capital, epic, direction_word)
            if activity_level is None
            else None
        )
        close_level = activity_level or market_level or confirm_level
        close_source = (
            "activity"
            if activity_level
            else ("market" if market_level else "confirm")
        )

        # Se Capital non ha popolato pnl, ricavalo da (close-entry)*size con
        # segno per direzione. Il prodotto e' in VALUTA QUOTATA: lo convertiamo
        # al riferimento USD (stesso del sizing) cosi' P&L, R e cap drawdown
        # sono nella stessa unita' di tutti gli altri asset. USD/sconosciuto ->
        # fattore 1.0 (bit-identico); HKD/JPY -> convertito (fix #84 Hang Seng:
        # 37.99 HKD veniva salvato come 37.99 EUR). Se il tasso non e'
        # disponibile NON blocchiamo la registrazione del close: meglio un pnl
        # non convertito che un close non registrato.
        if pnl is None and close_level and entry and size:
            delta = close_level - entry
            if direction_word == "short":
                delta = -delta
            pnl_quote = delta * size
            fac = 1.0
            try:
                from .risk import quote_to_ref_factor

                mkt = capital.get_market(epic) if epic else {}
                quote_ccy = (mkt.get("instrument") or {}).get("currency")
                f = quote_to_ref_factor(quote_ccy, capital)
                if f is not None:
                    fac = f
                elif quote_ccy and quote_ccy != "USD":
                    log.warning(
                        "pnl: tasso %s->USD non disponibile per %s, valore NON "
                        "convertito (in valuta quotata)", quote_ccy, epic,
                    )
            except Exception:
                log.warning("pnl: conversione quote->USD fallita per %s", epic)
            pnl = round(pnl_quote * fac, 4)

        asset_name = trade.get("asset") if trade else deal_id
        if trade:
            pnl_pct = (
                ((close_level - entry) / entry * 100) if entry else None
            )
            if direction_word == "short" and pnl_pct is not None:
                pnl_pct = -pnl_pct
            db.close_trade(
                deal_id,
                close_price=close_level,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=f"manual:{reason[:80]}" if reason else "manual",
            )
            db.insert_monitoring_event(
                {
                    "trade_id": trade["id"],
                    "event_type": "manual_close",
                    "reason": reason,
                    "details": {
                        "close_level": close_level,
                        "pnl": pnl,
                        "close_source": close_source,
                        "confirm_level": confirm_level,
                    },
                }
            )

        log.info(
            "Close %s: source=%s close_level=%s confirm_level=%s pnl=%s",
            asset_name,
            close_source,
            close_level,
            confirm_level,
            pnl,
        )

        text = (
            f"✅ <b>Posizione chiusa</b>\n"
            f"Asset: <b>{_esc(asset_name)}</b>\n"
            f"Prezzo chiusura: <code>{close_level}</code>\n"
            f"P&amp;L: <code>{pnl}</code>\n"
            f"Motivo: <i>{_esc(reason)}</i>"
        )
        if message_id:
            telegram.edit_message_text(message_id, text)
        else:
            telegram.send_message(text)
    except Exception as exc:
        log.exception("Chiusura fallita")
        telegram.send_message(
            f"⚠️ <b>Chiusura fallita</b>\n<i>{_esc(exc)}</i>"
        )


def run_trailing_stops(config: Config) -> None:
    """Versione leggera del monitor: applica solo il trailing stop a
    tutte le posizioni aperte. Nessuna chiamata LLM, solo aritmetica e
    ``update_position`` quando uno SL va spostato. Pensato per girare
    spesso (ogni 5 min, H24) per proteggere rapidamente il breakeven
    senza incidere sui costi.

    NIENTE check quiet_hours: la protezione dello SL e' migliorativa
    per definizione (uno SL viene spostato solo se piu' protettivo del
    precedente) e crypto/forex sono aperti H24, quindi blocchiamo solo
    quando non c'e' niente da fare (no posizioni)."""
    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)

    capital.login()
    positions = capital.get_open_positions()
    if not positions:
        return

    log.info(
        "Trailing scan su %d posizioni (step_r=%.2f)",
        len(positions),
        config.trailing_step_r,
    )
    for position in positions:
        try:
            _apply_trailing_stop(
                capital, db, telegram, position,
                step_r=config.trailing_step_r,
                v1_lowband=config.trail_v1_lowband,
                v2_highband=config.trail_v2_highband,
            )
        except Exception:
            log.exception("Trailing SL fallito")


def monitor_positions(config: Config) -> None:
    if is_quiet_now():
        log.info("Skip monitor: %s", quiet_reason())
        return

    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)

    capital.login()
    open_positions = capital.get_open_positions()

    # Reconcile inline: chiude in DB i trade che il broker ha gia' chiuso
    # (stop, tp, chiusura manuale dal frontend). Riusa la sessione Capital
    # e le live_positions appena recuperate per evitare HTTP duplicati.
    # Il job h24 jobs.reconcile resta come safety net fuori dalla finestra
    # attiva del monitor (notte e prima/dopo 7-22).
    try:
        from .reconcile import reconcile_open_trades

        rec = reconcile_open_trades(
            config,
            capital=capital,
            db=db,
            live_positions=open_positions,
            telegram=telegram,
        )
        if rec.get("closed", 0) > 0:
            log.info(
                "[reconcile] inline: checked=%d stale=%d closed=%d with_pnl=%d",
                rec.get("checked", 0),
                rec.get("stale", 0),
                rec.get("closed", 0),
                rec.get("closed_with_pnl", 0),
            )
    except Exception:
        log.exception("Reconcile inline fallito (continuo col monitor)")

    if not open_positions:
        log.info("Nessuna posizione aperta, nulla da monitorare")
        return

    log.info("Monitor su %d posizioni aperte", len(open_positions))

    for position in open_positions:
        # 1. Trailing stop (server-side) prima della valutazione LLM
        try:
            _apply_trailing_stop(
                capital, db, telegram, position,
                step_r=config.trailing_step_r,
                v1_lowband=config.trail_v1_lowband,
                v2_highband=config.trail_v2_highband,
            )
        except Exception:
            log.exception("Trailing SL fallito")

        # 2. Valutazione LLM per eventuale proposta di chiusura
        try:
            decision = _evaluate_position(config, capital, db, position)
        except Exception:
            log.exception("Valutazione posizione fallita")
            continue

        if not decision:
            continue
        action = (decision.get("action") or "").upper()
        log.info(
            "Decision %s su %s: %s (%s)",
            action,
            decision.get("asset"),
            decision.get("urgency"),
            decision.get("reason", "")[:80],
        )
        if action != "CLOSE":
            continue

        deal_id = decision.get("deal_id")
        if not deal_id:
            continue

        # Non proporre CLOSE se il mercato non e' attualmente tradeable:
        # l'utente non potrebbe eseguire nemmeno cliccando, e la proposta
        # tornerebbe a ogni run finche' il mercato riapre (loop di alert).
        market = position.get("market", {}) or {}
        market_status = (market.get("marketStatus") or "").upper()
        if market_status and market_status not in ("TRADEABLE",):
            log.info(
                "Skip alert CLOSE su %s: mercato %s, attendere riapertura",
                decision.get("asset"),
                market_status,
            )
            continue

        # AUTO-CLOSE (gated): simmetrico all'auto-confirm dell'apertura.
        # Se acceso, invia la proposta con solo il veto "Lascia aperta",
        # attende la finestra e auto-chiude se nessuno veta. Se spento,
        # comportamento storico: bottoni mclose/mhold, attesa click manuale.
        if getattr(config, "auto_close_enabled", False):
            _auto_close_with_veto(config, capital, db, telegram, deal_id, decision)
            continue

        # Invia proposta con bottoni mclose/mhold e ritorna.
        # Il daemon listener gestira' il click (confirm_handler).
        buttons = [
            [
                {
                    "text": "✅ Chiudi ora",
                    "callback_data": f"mclose:{deal_id}",
                },
                {
                    "text": "⏸ Lascia aperta",
                    "callback_data": f"mhold:{deal_id}",
                },
            ]
        ]
        telegram.send_message_with_buttons(
            _format_close_proposal(decision), buttons
        )
