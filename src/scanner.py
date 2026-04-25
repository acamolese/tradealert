"""Scanner orchestrator: fetch dati, calcolo feature, scoring LLM,
persistenza segnali e notifica Telegram.

Fase 1 (Coach): genera proposta, salva nel DB come 'pending', notifica utente.
L'esecuzione effettiva e' manuale sull'app Capital.com.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .capital_client import CapitalClient
from .config import Config
from .db import Database
from .discovery import discover_top_movers
from .executor import ExecutionResult
from .features import compute_features
from .llm_analyzer import LLMAnalyzer, SetupProposal
from .news import (
    fetch_finnhub_company_news,
    fetch_news,
    news_for_asset,
)
from .quiet_hours import is_quiet_now, quiet_reason
from .risk import SizingResult, calculate_size
from .telegram_client import TelegramClient
from .universe import UNIVERSE, Asset

log = logging.getLogger(__name__)


_FEATURE_REQUEST_DELAY_SEC = 0.15


# Weekend: solo crypto major. Esclude alt-coin micro-cap (BOBA, BLUR, GTC,
# BOME, GRIFFAIN, MERL, GUN, CPOOL, API3, AXS, ecc.) pescate dal Capital
# /marketnavigation perche' su micro-capitale il loro spread + slippage +
# pattern pump/dump produce winrate empirico zero (vedi analisi 2026-04-25,
# 5 chiusi 0 win -2.19 EUR). Le 4 alt-coin secondary in universe.py
# (Ethereum Classic, EthereumFi, EthereumPoW, ARPA) sono escluse dal weekend
# perche' non sono tra le 9 major elencate qui sotto.
WEEKEND_CRYPTO_MAJOR_ALLOWLIST = frozenset({
    "Bitcoin",
    "Ethereum",
    "Solana",
    "Ripple",
    "Cardano",
    "Avalanche",
    "Polkadot",
    "Chainlink",
    "Dogecoin",
})


def _filter_weekend_allowlist(
    features: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Restringe il bacino asset weekend ai soli 9 crypto major.
    Ritorna (filtered, removed_assets) per logging."""
    kept: dict[str, dict[str, Any]] = {}
    removed: list[str] = []
    for name, af in features.items():
        if name in WEEKEND_CRYPTO_MAJOR_ALLOWLIST:
            kept[name] = af
        else:
            removed.append(name)
    return kept, removed


def _proposals_summary(
    proposals: list[SetupProposal] | None,
) -> list[dict[str, Any]]:
    """Compatta i proposals del LLM per il logging in scanner_runs.notes:
    asset, direction, score e thesis troncata. Utile per analisi
    retrospettive (capire perche' un asset non e' stato promosso)."""
    if not proposals:
        return []
    out: list[dict[str, Any]] = []
    for p in proposals:
        thesis = p.thesis or ""
        if len(thesis) > 180:
            thesis = thesis[:177] + "..."
        out.append(
            {
                "asset": p.asset,
                "direction": p.direction,
                "score": p.score,
                "thesis": thesis,
            }
        )
    return out


def _log_run(
    db: Database,
    outcome: str,
    *,
    top_asset: str | None = None,
    top_score: float | None = None,
    candidates_count: int | None = None,
    open_positions_count: int | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    """Persiste l'esito della run dello scanner. Silenzia eventuali errori
    DB per non impattare la logica principale."""
    try:
        db.insert_scanner_run(
            {
                "outcome": outcome,
                "top_asset": top_asset,
                "top_score": top_score,
                "candidates_count": candidates_count,
                "open_positions_count": open_positions_count,
                "notes": notes,
            }
        )
    except Exception:
        log.exception("insert_scanner_run fallito (outcome=%s)", outcome)


def _collect_features(
    capital: CapitalClient, assets: list[Asset]
) -> dict[str, dict[str, Any]]:
    """Per ogni asset, fetcha candele 4H e snapshot. Salta i fallimenti.
    Throttle tra asset per restare sotto il rate limit Capital."""
    features: dict[str, dict[str, Any]] = {}
    for asset in assets:
        try:
            candles = capital.get_prices(
                asset.epic, resolution="HOUR_4", max_bars=60
            )
            snapshot = capital.get_market(asset.epic)
            if not candles:
                log.warning("Nessuna candela per %s (%s)", asset.name, asset.epic)
                time.sleep(_FEATURE_REQUEST_DELAY_SEC)
                continue
            features[asset.name] = compute_features(
                asset.name, candles, snapshot=snapshot
            )
            features[asset.name]["asset_class"] = asset.asset_class
            features[asset.name]["epic"] = asset.epic
        except Exception as exc:  # rete, epic invalido, rate limit
            log.warning(
                "Skip %s (%s): %s", asset.name, asset.epic, exc
            )
        time.sleep(_FEATURE_REQUEST_DELAY_SEC)
    return features


def _esc(text: object) -> str:
    """Escape minimo per Telegram parse_mode=HTML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _direction_label(direction: str, short: bool = False) -> str:
    """Etichetta esplicita per la direzione del trade.

    ``short=False`` restituisce la versione completa in italiano con
    indicazione del payoff (usata nei messaggi di setup e conferma).
    ``short=True`` restituisce la versione compatta con freccia (usata
    negli stati intermedi e nei riepiloghi di esecuzione).
    """
    d = (direction or "").lower()
    if d == "long":
        return (
            "🟢 LONG ↑" if short
            else "🟢 <b>LONG</b> — guadagno se il prezzo SALE ↑"
        )
    if d == "short":
        return (
            "🔴 SHORT ↓" if short
            else "🔴 <b>SHORT</b> — guadagno se il prezzo SCENDE ↓"
        )
    return d.upper()


def _format_macro_events_block(
    events: list[dict[str, Any]] | None,
) -> str:
    """Blocco informativo su eventi macro imminenti per l'asset.
    Il LLM dovrebbe gia' citarli nei risks, ma li stampiamo comunque
    qui in modo strutturato cosi' l'utente non deve fidarsi solo della
    thesis per avere il quadro."""
    if not events:
        return ""
    lines: list[str] = ["<b>📅 Contesto macro entro 24h</b>"]
    for ev in events[:4]:
        hours = ev.get("hours_until", "?")
        if ev.get("source") == "critical":
            desc = ev.get("description", "?")
            hint = ev.get("direction_hint", "unknown")
            lines.append(f"  • {_esc(desc)} tra {hours}h — <i>{hint}</i>")
        else:
            country = ev.get("country", "")
            name = ev.get("event", "?")
            est = ev.get("estimate")
            prev = ev.get("prev")
            est_block = f" (est {est} vs prev {prev})" if est is not None else ""
            lines.append(
                f"  • {country} {_esc(name)} tra {hours}h{est_block}"
            )
    return "\n".join(lines)


def _format_telegram_message(
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    execution_mode: str = "coach",
    macro_events_near: list[dict[str, Any]] | None = None,
) -> str:
    last = asset_features.get("last_price")
    spread = asset_features.get("spread_pct")
    direction_line = _direction_label(proposal.direction)

    sl_line = ""
    tp_line = ""
    if last and proposal.suggested_stop_pct and proposal.suggested_target_pct:
        if proposal.direction == "long":
            sl = last * (1 - proposal.suggested_stop_pct / 100)
            tp = last * (1 + proposal.suggested_target_pct / 100)
        else:
            sl = last * (1 + proposal.suggested_stop_pct / 100)
            tp = last * (1 - proposal.suggested_target_pct / 100)
        rr = proposal.suggested_target_pct / proposal.suggested_stop_pct
        sl_line = f"SL: <code>{sl:.5g}</code> (-{proposal.suggested_stop_pct}%)\n"
        tp_line = (
            f"TP: <code>{tp:.5g}</code> (+{proposal.suggested_target_pct}%) "
            f"R:R {rr:.1f}\n"
        )

    spread_line = f"Spread: {spread}%\n" if spread is not None else ""
    if execution_mode == "auto":
        footer = (
            "<i>Modalita auto: tra qualche istante ricevi l'esito "
            "dell'apertura su Capital.com.</i>"
        )
    elif execution_mode == "confirm":
        footer = (
            "<i>Modalita confirm: il trade e' preparato ma in attesa di "
            "tua autorizzazione esplicita (vedi messaggio successivo).</i>"
        )
    else:
        footer = (
            "<i>Esegui manualmente su Capital.com (modalita Coach).</i>"
        )

    macro_block = _format_macro_events_block(macro_events_near)
    macro_section = f"\n{macro_block}\n" if macro_block else ""

    return (
        f"🎯 <b>Setup individuato</b>\n\n"
        f"<b>{_esc(proposal.asset)}</b>  (score {proposal.score}/10)\n"
        f"{direction_line}\n"
        f"Prezzo: <code>{_esc(last)}</code>\n"
        f"{spread_line}"
        f"{sl_line}"
        f"{tp_line}\n"
        f"<i>Thesis:</i>\n{_esc(proposal.thesis)}\n"
        f"{macro_section}\n"
        f"{footer}"
    )


def _min_entry_eur(asset_features: dict[str, Any]) -> float | None:
    """Margine minimo (EUR) per aprire la size minima del broker su questo asset.

    Calcolo: min_size * entry_price * margin_factor.
    Restituisce None se i dati di mercato non sono disponibili.
    """
    last = asset_features.get("last_price")
    min_size = asset_features.get("min_size")
    margin_factor = asset_features.get("margin_factor")
    if not (last and min_size and margin_factor):
        return None
    return float(min_size) * float(last) * float(margin_factor)


def _format_reasoning_block(
    proposal: SetupProposal, asset_features: dict[str, Any]
) -> str:
    """Compone il blocco Ragionamento + key_factors + risks + news per
    il messaggio Telegram di conferma."""
    parts: list[str] = [f"<i>Thesis:</i>\n{_esc(proposal.thesis)}"]
    if proposal.key_factors:
        parts.append(
            "<i>Fattori chiave:</i>\n"
            + "\n".join(f"• {_esc(f)}" for f in proposal.key_factors[:4])
        )
    if proposal.risks:
        parts.append(
            "<i>Rischi:</i>\n"
            + "\n".join(f"⚠️ {_esc(r)}" for r in proposal.risks[:2])
        )
    news = asset_features.get("news") or []
    if news:
        parts.append(
            "<i>News recenti:</i>\n"
            + "\n".join(
                f"📰 {_esc((n.get('headline') or '')[:100])}"
                for n in news[:3]
            )
        )
    return "\n\n".join(parts)


def _format_confirm_message(
    signal_row: dict[str, Any],
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    current_budget: float,
    sizing: "SizingResult",
    timeout_sec: int,
    macro_events_near: list[dict[str, Any]] | None = None,
) -> str:
    if sizing.size is None:
        sizing_block = (
            f"⚠️ <i>Con budget {current_budget:.0f} EUR il sizing non passa: "
            f"{_esc(sizing.reason)}</i>"
        )
    else:
        sizing_block = (
            f"<b>Preview con budget {current_budget:.0f} EUR (= margine sul conto):</b>\n"
            f"Margine bloccato: <code>{sizing.margin_estimate:.2f} EUR</code>\n"
            f"Size: <code>{sizing.size:g}</code>\n"
            f"Esposizione generata: <code>{sizing.notional:.2f} EUR</code>\n"
            f"Rischio se SL: <code>{sizing.risk_estimate:.2f} EUR</code>"
        )
    reasoning = _format_reasoning_block(proposal, asset_features)
    macro_block = _format_macro_events_block(macro_events_near)
    macro_section = f"\n\n{macro_block}" if macro_block else ""
    return (
        f"🟡 <b>Conferma richiesta</b> (signal {signal_row['id']})\n\n"
        f"<b>{_esc(proposal.asset)}</b>  (score {proposal.score}/10)\n"
        f"{_direction_label(proposal.direction)}\n\n"
        f"{reasoning}\n\n"
        f"{sizing_block}"
        f"{macro_section}\n\n"
        f"<i>I bottoni sotto sono l'importo in EUR da bloccare come margine. "
        f"Clicca un valore poi Esegui.</i>"
    )


def _confirm_buttons(
    signal_id: int,
    current_budget: float,
    budget_options: list[float],
    min_entry: float | None = None,
) -> list[list[dict[str, str]]]:
    options = [int(round(b)) for b in budget_options]
    # Aggiungi bottone "minimo" se diverso dai preset standard
    if min_entry is not None:
        rounded_min = max(1, int(round(min_entry)))
        if rounded_min not in options:
            options = [rounded_min] + options
        options = sorted(set(options))

    budget_row = []
    for b in options:
        label = f"€{b}"
        if min_entry is not None and abs(b - round(min_entry)) < 0.5:
            label += " min"
        if abs(b - current_budget) < 0.5:
            label += " ✓"
        budget_row.append(
            {"text": label, "callback_data": f"budget:{b}:{signal_id}"}
        )

    # Se i bottoni sono troppi, splittali in due righe
    rows = []
    if len(budget_row) > 5:
        mid = (len(budget_row) + 1) // 2
        rows.append(budget_row[:mid])
        rows.append(budget_row[mid:])
    else:
        rows.append(budget_row)

    rows.append(
        [
            {
                "text": "✅ Esegui",
                "callback_data": f"exec:{signal_id}:{int(current_budget)}",
            },
            {"text": "❌ Salta", "callback_data": f"skip:{signal_id}"},
        ]
    )
    return rows


def _format_execution_message(
    result: "ExecutionResult", proposal: SetupProposal
) -> str:
    if result.executed:
        return (
            f"✅ <b>Posizione aperta su Capital.com</b>\n\n"
            f"<b>{_esc(proposal.asset)}</b> {_direction_label(proposal.direction, short=True)}\n"
            f"Size: <code>{result.size}</code>\n"
            f"Entry: <code>{result.entry_price}</code>\n"
            f"SL: <code>{result.stop_level}</code>\n"
            f"TP: <code>{result.profit_level}</code>\n"
            f"Deal ID: <code>{_esc(result.deal_id)}</code>"
        )
    return (
        f"⚠️ <b>Esecuzione saltata</b>\n\n"
        f"Setup <b>{_esc(proposal.asset)}</b> identificato ma non eseguito.\n"
        f"Motivo: <i>{_esc(result.reason)}</i>\n\n"
        f"<i>Suggerimento: il setup resta nel DB come pending, "
        f"puoi rivalutare manualmente o aspettare il prossimo scan.</i>"
    )


def _prefilter_candidates(
    features: dict[str, dict[str, Any]],
    min_candidates: int = 3,
    fallback_n: int = 5,
    max_candidates: int = 8,
    is_weekend: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Filtro deterministico pre-LLM per ridurre i token.

    Passa solo asset che soddisfano almeno uno di questi criteri:
      - abs(daily_pct_change) >= 2 (movimento forte oggi)
      - rsi_14 <= 30 o >= 70 (estremi momentum)
      - abs(pct_from_high_20) <= 1 (lateralita' vicino ai massimi =
        potenziale breakout) oppure pct_from_high_20 <= -8 (correzione
        profonda = potenziale bounce)
      - bb_width_pct <= 1.5 (compressione volatilita')

    Cintura di sicurezza weekend (``is_weekend=True``): blocca a monte i
    pattern blow-off top, ovvero asset con ``rsi_14 > 75`` e
    ``abs(daily_pct_change) > 15`` simultaneamente. Statisticamente sono
    mean reversion ad alta probabilita', non continuation: vale anche per
    BTC/ETH (un +20% intraday su BTC con RSI 80 e' blow-off come su una
    micro-cap). counters["blow_off_blocked"] li conta.

    Safety net: se il filtro lascia < ``min_candidates`` asset (giornata
    piatta), passa i top ``fallback_n`` per abs(daily_pct_change) come
    fallback. Evita di "spegnere" lo scanner nei giorni morti.

    Cap superiore: mai oltre ``max_candidates`` asset (default 8) alla
    LLM per contenere i token di input. Se il filtro produce di piu',
    si tengono i top ``max_candidates`` per abs(daily_pct_change) e
    counters["pre_filter_capped"] segnala l'evento (per tunare il
    cap a posteriori).

    Ritorna (filtered, counters) dove counters ha i motivi per cui
    ogni asset e' passato: utile per tunare le soglie a posteriori.
    """
    selected: dict[str, dict[str, Any]] = {}
    counters: dict[str, int] = {
        "daily_pct": 0,
        "rsi_extreme": 0,
        "near_high": 0,
        "correction": 0,
        "bb_compression": 0,
    }

    # Blow-off top filter: gira PRIMA dei criteri di passaggio cosi' un
    # asset blow-off non puo' nemmeno passare via daily_pct >= 2.
    if is_weekend:
        counters["blow_off_blocked"] = 0
        kept_features: dict[str, dict[str, Any]] = {}
        for name, af in features.items():
            rsi = af.get("rsi_14")
            daily = af.get("daily_pct_change")
            try:
                rsi_f = float(rsi) if rsi is not None else None
                daily_f = float(daily) if daily is not None else None
            except (TypeError, ValueError):
                rsi_f, daily_f = None, None
            if (
                rsi_f is not None
                and daily_f is not None
                and rsi_f > 75
                and abs(daily_f) > 15
            ):
                counters["blow_off_blocked"] += 1
                log.info(
                    "[blow_off_top] blocco %s: rsi=%.1f daily_pct=%+.1f",
                    name, rsi_f, daily_f,
                )
                continue
            kept_features[name] = af
        features = kept_features

    for name, af in features.items():
        reasons: list[str] = []
        daily = af.get("daily_pct_change")
        if daily is not None and abs(float(daily)) >= 2.0:
            reasons.append("daily_pct")
        rsi = af.get("rsi_14")
        if rsi is not None and (float(rsi) <= 30 or float(rsi) >= 70):
            reasons.append("rsi_extreme")
        pct_high = af.get("pct_from_high_20")
        if pct_high is not None:
            pct = float(pct_high)
            if abs(pct) <= 1.0:
                reasons.append("near_high")
            elif pct <= -8.0:
                reasons.append("correction")
        bb = af.get("bb_width_pct")
        if bb is not None and float(bb) <= 1.5:
            reasons.append("bb_compression")

        if reasons:
            selected[name] = af
            for r in reasons:
                counters[r] += 1

    # Safety net: se troppo pochi, prendi i top per daily_pct_change
    if len(selected) < min_candidates:
        ranked = sorted(
            features.items(),
            key=lambda kv: abs(float(kv[1].get("daily_pct_change") or 0)),
            reverse=True,
        )
        for name, af in ranked[:fallback_n]:
            if name not in selected:
                selected[name] = af
        counters["fallback_used"] = 1

    # Cap superiore: oltre max_candidates tieni solo i top per daily_pct_change.
    if len(selected) > max_candidates:
        capped = sorted(
            selected.items(),
            key=lambda kv: abs(float(kv[1].get("daily_pct_change") or 0)),
            reverse=True,
        )[:max_candidates]
        selected = dict(capped)
        counters["pre_filter_capped"] = 1

    return selected, counters


def _pick_top_setup(
    proposals: list[SetupProposal],
    features: dict[str, dict[str, Any]],
    min_score: float,
    exclude_assets: set[str] | None = None,
    max_affordable_eur: float | None = None,
    skipped_reasons: list[str] | None = None,
) -> SetupProposal | None:
    """Ritorna il primo setup sopra soglia con mercato aperto e tradeable.
    Scarta asset gia' proposti di recente (``exclude_assets``), e scarta
    i setup con ``min_entry`` broker sopra ``max_affordable_eur`` (se
    fornito) per non proporre strumenti troppo cari rispetto ai budget
    preset. Le motivazioni dei setup scartati per accessibilita' vengono
    accodate a ``skipped_reasons`` (se passato) per essere mostrate
    all'utente nel messaggio 'no setup'."""
    exclude_assets = exclude_assets or set()
    for p in proposals:
        if p.score < min_score:
            continue
        if p.asset in exclude_assets:
            continue
        af = features.get(p.asset)
        if not af or not af.get("last_price"):
            continue
        status = af.get("market_status", "UNKNOWN")
        if status not in ("TRADEABLE", "UNKNOWN"):
            continue  # CLOSED, SUSPENDED, OFFLINE...
        if max_affordable_eur is not None:
            min_entry = _min_entry_eur(af)
            if min_entry is not None and min_entry > max_affordable_eur:
                if skipped_reasons is not None:
                    skipped_reasons.append(
                        f"{p.asset}: servono ~{min_entry:.0f} EUR di margine "
                        f"minimo (tuo budget max {max_affordable_eur:.0f} EUR)"
                    )
                continue
        return p
    return None


def _format_no_setup_message(
    proposals: list[SetupProposal], skipped_reasons: list[str] | None = None
) -> str:
    top = proposals[0] if proposals else None
    skipped_block = ""
    if skipped_reasons:
        skipped_block = (
            "\n\n<b>Setup scartati per accessibilita' col tuo budget:</b>\n"
            + "\n".join(f"- {_esc(r)}" for r in skipped_reasons[:5])
        )
    if top:
        return (
            f"📭 <b>Nessun setup eseguibile oggi</b>\n\n"
            f"Top candidato: <b>{_esc(top.asset)}</b> "
            f"score {top.score}/10\n"
            f"<i>{_esc(top.thesis)}</i>"
            f"{skipped_block}"
        )
    return (
        f"📭 <b>Nessun setup oggi</b>\n\n"
        f"LLM non ha prodotto candidati sopra soglia.{skipped_block}"
    )


def _preview_sizing(
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    margin_budget: float,
) -> SizingResult:
    return calculate_size(
        margin_budget=margin_budget,
        entry_price=asset_features.get("last_price") or 0,
        margin_factor=asset_features.get("margin_factor") or 0.05,
        min_size=asset_features.get("min_size") or 0.01,
        size_step=asset_features.get("size_step")
        or asset_features.get("min_size")
        or 0.01,
        stop_pct=proposal.suggested_stop_pct or 1.5,
    )


ROTATION_DELTA = 2.0  # delta default (posizione aperta in profit o neutra)
ROTATION_DELTA_SOFT_DD = 1.0   # se la posizione aperta peggiore e' in DD >= -0.5%
ROTATION_DELTA_HARD_DD = 0.5   # se la posizione aperta peggiore e' in DD >= -1%


def _position_pnl_pct(pos: dict[str, Any], market: dict[str, Any]) -> float:
    """Stima del P&L% corrente basata su bid/offer medio e direzione."""
    entry = pos.get("level")
    direction = pos.get("direction")
    bid = market.get("bid")
    offer = market.get("offer")
    if entry is None or not direction or bid is None or offer is None:
        return 0.0
    mid = (float(bid) + float(offer)) / 2
    entry_f = float(entry)
    if entry_f <= 0:
        return 0.0
    if direction == "BUY":
        return (mid - entry_f) / entry_f * 100
    return (entry_f - mid) / entry_f * 100


def _maybe_build_rotation_proposal(
    config: Config,
    capital: CapitalClient,
    db: Database,
    top: SetupProposal,
    signal_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Se MAX_OPEN_POSITIONS e' saturo valuta una rotation. La soglia di
    delta score richiesto e' adattiva rispetto al P&L% della posizione
    peggiore:
    - DD >= -1%  -> basta +0.5 score
    - DD >= -0.5%-> basta +1.0 score
    - profit/neutra -> serve +2.0 score (default)
    """
    try:
        open_positions = capital.get_open_positions()
    except Exception:
        log.exception("Rotation: fetch posizioni aperte fallito")
        return None
    if len(open_positions) < config.max_open_positions:
        return None

    candidates: list[dict[str, Any]] = []
    for wrapper in open_positions:
        pos = wrapper.get("position", {}) or {}
        market = wrapper.get("market", {}) or {}
        deal_id = pos.get("dealId")
        if not deal_id:
            continue
        trade = db.get_trade_by_deal_id(deal_id)
        if not trade or not trade.get("signal_id"):
            continue
        sig = db.get_signal(trade["signal_id"])
        if not sig:
            continue
        try:
            orig_score = float(sig.get("score") or 0)
        except (TypeError, ValueError):
            continue
        pnl_pct = _position_pnl_pct(pos, market)
        candidates.append(
            {
                "deal_id": deal_id,
                "asset": sig.get("asset") or "?",
                "orig_score": orig_score,
                "pnl_pct": pnl_pct,
            }
        )

    if not candidates:
        return None

    # Candidato preferito per chiusura: score originale piu' basso, a parita'
    # PnL% piu' basso (tecnicamente mediocre E in perdita = prime a uscire).
    candidates.sort(key=lambda c: (c["orig_score"], c["pnl_pct"]))
    worst = candidates[0]

    if worst["pnl_pct"] <= -1.0:
        required_delta = ROTATION_DELTA_HARD_DD
    elif worst["pnl_pct"] <= -0.5:
        required_delta = ROTATION_DELTA_SOFT_DD
    else:
        required_delta = ROTATION_DELTA

    if top.score - worst["orig_score"] < required_delta:
        return None

    return {
        "deal_id": worst["deal_id"],
        "asset": worst["asset"],
        "score": worst["orig_score"],
        "pnl_pct": round(worst["pnl_pct"], 2),
        "delta": round(top.score - worst["orig_score"], 1),
        "required_delta": required_delta,
    }


def _format_rotation_message(
    signal_row: dict[str, Any],
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    rotation: dict[str, Any],
) -> str:
    reasoning = _format_reasoning_block(proposal, asset_features)
    pnl = rotation.get("pnl_pct")
    pnl_str = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "-"
    req = rotation.get("required_delta", ROTATION_DELTA)
    return (
        f"🔄 <b>Proposta di rotation</b> (signal {signal_row['id']})\n\n"
        f"<b>Nuovo setup:</b> {_esc(proposal.asset)} (score {proposal.score}/10)\n"
        f"{_direction_label(proposal.direction)}\n"
        f"<b>Posizione da chiudere:</b> {_esc(rotation['asset'])}\n"
        f"  score originale: <code>{rotation['score']}/10</code>\n"
        f"  P&amp;L attuale: <code>{pnl_str}</code>\n"
        f"  soglia richiesta per switch: <code>+{req}</code> "
        f"(delta effettivo +{rotation['delta']})\n\n"
        f"{reasoning}\n\n"
        f"<i>Scegli:</i>\n"
        f"✅ Ruota: chiude {_esc(rotation['asset'])} e apre "
        f"{_esc(proposal.asset)}\n"
        f"🔄 Solo apri: ignora la vecchia (ma MAX_OPEN limita)\n"
        f"❌ Ignora: nessuna azione"
    )


def _rotation_buttons(
    signal_id: int, old_deal_id: str
) -> list[list[dict[str, str]]]:
    return [
        [
            {
                "text": "✅ Ruota",
                "callback_data": f"rot:exec:{signal_id}:{old_deal_id}",
            },
            {
                "text": "🔄 Solo apri",
                "callback_data": f"rot:open:{signal_id}",
            },
        ],
        [
            {
                "text": "❌ Ignora",
                "callback_data": f"rot:skip:{signal_id}",
            }
        ],
    ]


def _handle_confirm(
    config: Config,
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    signal_row: dict[str, Any],
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    macro_events_near: list[dict[str, Any]] | None = None,
) -> None:
    """Invia il messaggio di conferma con bottoni e ritorna. I callback
    dei bottoni (Esegui/Salta/Budget) sono gestiti in modo asincrono dal
    listener daemon (``src.confirm_handler``), cosi' nessun polling
    Telegram concorrente.
    """
    signal_id = signal_row["id"]
    current_budget = float(config.exposure_budget_eur)
    sizing = _preview_sizing(proposal, asset_features, current_budget)

    telegram.send_message_with_buttons(
        _format_confirm_message(
            signal_row,
            proposal,
            asset_features,
            current_budget,
            sizing,
            config.confirm_timeout_sec,
            macro_events_near=macro_events_near,
        ),
        _confirm_buttons(
            signal_id,
            current_budget,
            config.budget_options,
            _min_entry_eur(asset_features),
        ),
    )


def run_morning_scan(config: Config) -> None:
    if is_quiet_now():
        log.info("Skip scan: %s", quiet_reason())
        return

    capital = CapitalClient(config)
    telegram = TelegramClient(config)
    db = Database(config)
    llm = LLMAnalyzer(config)

    log.info("Login Capital.com (env=%s)", config.capital_env)
    capital.login()

    log.info("Snapshot account")
    try:
        accounts = capital.get_account_info().get("accounts", [])
        if accounts:
            acc = accounts[0]
            db.insert_account_snapshot(
                {
                    "balance": acc.get("balance", {}).get("balance"),
                    "equity": acc.get("balance", {}).get("available"),
                    "open_positions": None,
                    "daily_pnl": acc.get("balance", {}).get("profitLoss"),
                }
            )
    except Exception as exc:
        log.warning("Snapshot account fallito: %s", exc)

    # Discovery dinamica: top mover letti direttamente da
    # /marketnavigation (crypto group + shares popolari). Copertura
    # ampia senza hardcoded watchlist.
    try:
        movers, mover_quotes = discover_top_movers(
            capital, top_n=7, abs_min_pct=2.0
        )
    except Exception:
        log.exception("Discovery fallita, uso solo universo statico")
        movers, mover_quotes = [], []

    scan_set: list[Asset] = list(UNIVERSE)
    seen_epics = {a.epic for a in scan_set}
    for a in movers:
        if a.epic not in seen_epics:
            scan_set.append(a)
            seen_epics.add(a.epic)
    log.info(
        "Scan set: %d asset (universo %d + discovery %d)",
        len(scan_set),
        len(UNIVERSE),
        len(scan_set) - len(UNIVERSE),
    )

    features = _collect_features(capital, scan_set)
    if not features:
        telegram.send_message(
            "⚠️ Scanner mattutino: nessun dato di mercato disponibile."
        )
        _log_run(db, "no_data", notes={"scan_set": len(scan_set)})
        return

    # Calcolo is_weekend una volta sola e lo riuso per: (a) restringere il
    # bacino asset weekend ai 9 crypto major, (b) attivare il filtro blow-off
    # nel pre-filter, (c) popolare il context per la LLM.
    from datetime import datetime as _dt, timezone as _tz
    is_weekend = _dt.now(_tz.utc).weekday() >= 5

    if is_weekend:
        pre_allowlist_n = len(features)
        features, removed_assets = _filter_weekend_allowlist(features)
        log.info(
            "[weekend_allowlist] bacino: %d -> %d (kept=%s removed=%d)",
            pre_allowlist_n,
            len(features),
            sorted(features.keys()),
            len(removed_assets),
        )
        if not features:
            telegram.send_message(
                "ℹ️ Scanner weekend: nessun crypto major nel bacino. Skip."
            )
            _log_run(
                db,
                "no_setup",
                notes={
                    "scan_set": len(scan_set),
                    "weekend_allowlist": {
                        "before": pre_allowlist_n,
                        "after": 0,
                    },
                },
            )
            return

    # Arricchimento con news per asset (RSS pubblici + Finnhub company-news
    # per le azioni). Le news vanno nel payload LLM come contesto per
    # validare o scartare un momentum senza catalyst.
    try:
        all_news = fetch_news(config, limit=40)
    except Exception:
        log.exception("fetch_news fallita, proseguo senza news globali")
        all_news = []
    for name, af in features.items():
        per_asset = news_for_asset(name, all_news, limit=3)
        if af.get("asset_class") == "share":
            try:
                per_asset = per_asset + fetch_finnhub_company_news(
                    config, af.get("epic") or "", limit=3
                )
            except Exception:
                log.exception("finnhub company-news fallita %s", name)
        af["news"] = [
            {
                "headline": n.get("headline"),
                "source": n.get("source"),
                "datetime": n.get("datetime"),
            }
            for n in per_asset[:3]
        ]

    log.info("Ranking LLM su %d asset", len(features))
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # is_weekend gia' calcolato in cima al flow per applicare l'allowlist;
    # qui lo riusiamo nel context senza ricalcolarlo.
    traditional_open = sum(
        1
        for name, af in features.items()
        if af.get("asset_class") != "crypto"
        and af.get("market_status") in ("TRADEABLE", "EDITS_ONLY")
    )
    from .market_context import get_critical_events, get_economic_calendar

    critical_events = get_critical_events(hours_ahead=72)
    economic_calendar = get_economic_calendar(config, hours_ahead=72)
    context = {
        "is_weekend": is_weekend,
        "weekday": now.strftime("%A"),
        "traditional_markets_open": traditional_open,
        "tradeable_count": sum(
            1
            for af in features.values()
            if af.get("market_status") in ("TRADEABLE", "EDITS_ONLY")
        ),
        "critical_events": critical_events,
        "economic_calendar": economic_calendar,
    }
    # Pre-filtro deterministico: riduce gli asset passati al LLM dai 33
    # tipici (universe + discovery) a 6-12 candidati "interessanti" secondo
    # regole tecniche. Abbatte i token di input del 60-70% con un safety
    # net che evita di spegnere lo scanner nei giorni piatti.
    pre_n = len(features)
    filtered_features, filter_counts = _prefilter_candidates(
        features, is_weekend=is_weekend
    )
    log.info(
        "Pre-filter: %d -> %d (%s)",
        pre_n,
        len(filtered_features),
        ", ".join(f"{k}={v}" for k, v in filter_counts.items() if v),
    )

    proposals = llm.rank_setups(filtered_features, context=context)

    # Guardrail deterministici: il prompt chiede al LLM di applicare
    # le regole macro, ma a volte le ignora quando il setup tecnico
    # gli piace. Qui le imponiamo in Python prima del ranking finale.
    from .macro_guard import apply_macro_guardrails

    try:
        open_position_assets = [
            (p.get("market", {}) or {}).get("instrumentName") or ""
            for p in capital.get_open_positions()
        ]
    except Exception:
        log.exception("Fetch open positions per guardrails fallito")
        open_position_assets = []

    proposals, guardrail_logs = apply_macro_guardrails(
        proposals,
        critical_events=critical_events,
        economic_calendar=economic_calendar,
        open_position_assets=open_position_assets,
    )
    for gl in guardrail_logs:
        log.info("Guardrail %s [%s]: %s", gl.action, gl.asset, gl.details)

    eligible = [p for p in proposals if p.direction in ("long", "short")]
    eligible.sort(key=lambda p: p.score, reverse=True)

    # Dedup giornaliero: non riproporre asset gia' segnalati nelle ultime 24h
    try:
        recent_assets = db.recent_signal_assets(hours=24)
    except Exception:
        log.exception("Lookup signal recenti fallito, skip dedup")
        recent_assets = set()

    max_affordable = (
        max(config.budget_options) if config.budget_options else None
    )
    skipped_reasons: list[str] = []
    top = _pick_top_setup(
        eligible,
        features,
        min_score=config.min_score_threshold,
        exclude_assets=recent_assets,
        max_affordable_eur=max_affordable,
        skipped_reasons=skipped_reasons,
    )

    if not top:
        # Silenzio: nessuna opportunita' nuova sopra soglia.
        log.info(
            "Scan: nessun top nuovo (recent_assets=%d, eligible=%d, "
            "scartati_per_budget=%d)",
            len(recent_assets),
            len(eligible),
            len(skipped_reasons),
        )
        for reason in skipped_reasons:
            log.info("  skip budget: %s", reason)
        best = eligible[0] if eligible else None
        _log_run(
            db,
            "no_setup",
            top_asset=best.asset if best else None,
            top_score=best.score if best else None,
            candidates_count=len(eligible),
            notes={
                "recent_dedup_count": len(recent_assets),
                "min_score_threshold": config.min_score_threshold,
                "scan_set": len(scan_set),
                "proposals": _proposals_summary(proposals),
                "skipped_for_budget": skipped_reasons,
                "pre_filter": {
                    "before": pre_n,
                    "after": len(filtered_features),
                    "reasons": filter_counts,
                },
            },
        )
        return

    # Se gli slot di posizione sono pieni, il signal ha senso SOLO se la
    # rotation scatta (nuovo score - peggiore aperta >= ROTATION_DELTA).
    # Altrimenti silenzio: eviteremmo comunque l'apertura all'executor.
    try:
        open_count = len(capital.get_open_positions())
    except Exception:
        log.exception("Fetch posizioni aperte fallito")
        open_count = 0

    asset_features = features.get(top.asset, {})
    signal_row = db.insert_signal(
        {
            "asset": top.asset,
            "epic": asset_features.get("epic"),
            "direction": top.direction,
            "score": top.score,
            "thesis": top.thesis,
            "entry_price": asset_features.get("last_price"),
            "stop_loss": top.suggested_stop_pct,
            "take_profit": top.suggested_target_pct,
            "size": None,
            "expected_cost": None,
            "status": "pending",
        }
    )
    log.info("Signal salvato id=%s", signal_row.get("id"))

    rotation = _maybe_build_rotation_proposal(
        config, capital, db, top, signal_row
    )
    if rotation:
        telegram.send_message_with_buttons(
            _format_rotation_message(
                signal_row, top, asset_features, rotation
            ),
            _rotation_buttons(signal_row["id"], rotation["deal_id"]),
        )
        _log_run(
            db,
            "rotation_proposed",
            top_asset=top.asset,
            top_score=top.score,
            candidates_count=len(eligible),
            open_positions_count=open_count,
            notes={
                "rotation_target": rotation.get("asset"),
                "rotation_delta": rotation.get("delta"),
                "proposals": _proposals_summary(proposals),
                "pre_filter": {
                    "before": pre_n,
                    "after": len(filtered_features),
                    "reasons": filter_counts,
                },
            },
        )
        return

    if open_count >= config.max_open_positions:
        # Slot pieni e nessuna rotation utile: silenzio, marcamo il signal
        # come expired cosi' non conta nel dedup come 'attivo'.
        log.info(
            "Slot pieni (%d/%d) e rotation non applicabile: skip notifica",
            open_count,
            config.max_open_positions,
        )
        db.update_signal_status(signal_row["id"], "expired")
        _log_run(
            db,
            "slots_full",
            top_asset=top.asset,
            top_score=top.score,
            candidates_count=len(eligible),
            open_positions_count=open_count,
            notes={
                "max_open_positions": config.max_open_positions,
                "proposals": _proposals_summary(proposals),
                "pre_filter": {
                    "before": pre_n,
                    "after": len(filtered_features),
                    "reasons": filter_counts,
                },
            },
        )
        return

    # Eventi macro rilevanti per l'asset scelto (stampati in modo
    # strutturato nei messaggi, oltre a quanto il LLM ha scritto).
    from .macro_guard import events_affecting_asset

    macro_events_near = events_affecting_asset(
        top.asset,
        critical_events=critical_events,
        economic_calendar=economic_calendar,
        within_hours=24.0,
    )
    telegram.send_message(
        _format_telegram_message(
            top,
            asset_features,
            execution_mode=config.execution_mode,
            macro_events_near=macro_events_near,
        )
    )
    _log_run(
        db,
        "signal_sent",
        top_asset=top.asset,
        top_score=top.score,
        candidates_count=len(eligible),
        open_positions_count=open_count,
        notes={
            "execution_mode": config.execution_mode,
            "proposals": _proposals_summary(proposals),
            "pre_filter": {
                "before": pre_n,
                "after": len(filtered_features),
                "reasons": filter_counts,
            },
        },
    )

    if config.execution_mode == "auto":
        from .executor import execute_signal

        log.info("EXECUTION_MODE=auto, tento esecuzione")
        result = execute_signal(
            config, capital, db, signal_row, asset_features
        )
        telegram.send_message(_format_execution_message(result, top))
    elif config.execution_mode == "confirm":
        log.info(
            "EXECUTION_MODE=confirm, signal id=%s in attesa di click",
            signal_row["id"],
        )
        _handle_confirm(
            config,
            capital,
            db,
            telegram,
            signal_row,
            top,
            asset_features,
            macro_events_near=macro_events_near,
        )
    else:
        log.info("EXECUTION_MODE=coach, solo notifica")
