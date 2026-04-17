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
from .executor import ExecutionResult
from .features import compute_features
from .llm_analyzer import LLMAnalyzer, SetupProposal
from .risk import SizingResult, calculate_size
from .telegram_client import TelegramClient
from .universe import UNIVERSE, Asset

log = logging.getLogger(__name__)


def _collect_features(
    capital: CapitalClient, assets: list[Asset]
) -> dict[str, dict[str, Any]]:
    """Per ogni asset, fetcha candele 4H e snapshot. Salta i fallimenti."""
    features: dict[str, dict[str, Any]] = {}
    for asset in assets:
        try:
            candles = capital.get_prices(
                asset.epic, resolution="HOUR_4", max_bars=60
            )
            snapshot = capital.get_market(asset.epic)
            if not candles:
                log.warning("Nessuna candela per %s (%s)", asset.name, asset.epic)
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
    return features


def _esc(text: object) -> str:
    """Escape minimo per Telegram parse_mode=HTML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_telegram_message(
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    execution_mode: str = "coach",
) -> str:
    last = asset_features.get("last_price")
    spread = asset_features.get("spread_pct")
    arrow = "🟢 LONG" if proposal.direction == "long" else "🔴 SHORT"

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

    return (
        f"🎯 <b>Setup del giorno</b>\n\n"
        f"<b>{_esc(proposal.asset)}</b> {arrow}  (score {proposal.score}/10)\n"
        f"Prezzo: <code>{_esc(last)}</code>\n"
        f"{spread_line}"
        f"{sl_line}"
        f"{tp_line}\n"
        f"<i>Thesis:</i>\n{_esc(proposal.thesis)}\n\n"
        f"{footer}"
    )


_BUDGET_OPTIONS = [10, 15, 20, 25, 30]


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


def _format_confirm_message(
    signal_row: dict[str, Any],
    proposal: SetupProposal,
    asset_features: dict[str, Any],
    current_budget: float,
    sizing: "SizingResult",
    timeout_sec: int,
) -> str:
    min_entry = _min_entry_eur(asset_features)
    min_line = (
        f"<b>Margine minimo per entrare:</b> <code>{min_entry:.2f} EUR</code>\n\n"
        if min_entry is not None
        else ""
    )

    if sizing.size is None:
        sizing_block = (
            f"⚠️ <i>Con budget {current_budget:.0f} EUR il sizing non passa: "
            f"{_esc(sizing.reason)}</i>"
        )
    else:
        sizing_block = (
            f"<b>Preview con budget {current_budget:.0f} EUR:</b>\n"
            f"Size: <code>{sizing.size:g}</code>\n"
            f"Margine impegnato: <code>{sizing.margin_estimate:.2f} EUR</code>\n"
            f"Rischio se SL: <code>{sizing.risk_estimate:.2f} EUR</code>\n"
            f"Notional: <code>{sizing.notional:.2f}</code>"
        )
    return (
        f"🟡 <b>Conferma richiesta</b> (signal {signal_row['id']})\n\n"
        f"<b>{_esc(proposal.asset)}</b> "
        f"{proposal.direction.upper()} (score {proposal.score}/10)\n\n"
        f"{min_line}"
        f"{sizing_block}\n\n"
        f"<i>Cambia budget con i bottoni o conferma. "
        f"Timeout {timeout_sec // 60} min.</i>"
    )


def _confirm_buttons(
    signal_id: int,
    current_budget: float,
    min_entry: float | None = None,
) -> list[list[dict[str, str]]]:
    options = list(_BUDGET_OPTIONS)
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
            f"<b>{_esc(proposal.asset)}</b> {proposal.direction.upper()}\n"
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


def _pick_top_setup(
    proposals: list[SetupProposal],
    features: dict[str, dict[str, Any]],
    min_score: float,
) -> SetupProposal | None:
    """Ritorna il primo setup sopra soglia con mercato aperto e tradeable.

    NON filtra in base al budget: la decisione di alzare il budget per
    rendere eseguibile un setup borderline e' lasciata all'utente nel
    flusso di conferma.
    """
    for p in proposals:
        if p.score < min_score:
            continue
        af = features.get(p.asset)
        if not af or not af.get("last_price"):
            continue
        status = af.get("market_status", "UNKNOWN")
        if status not in ("TRADEABLE", "UNKNOWN"):
            continue  # CLOSED, SUSPENDED, OFFLINE...
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


def _handle_confirm(
    config: Config,
    capital: CapitalClient,
    db: Database,
    telegram: TelegramClient,
    signal_row: dict[str, Any],
    proposal: SetupProposal,
    asset_features: dict[str, Any],
) -> None:
    """Loop interattivo: mostra setup con preview sizing e bottoni budget.
    L'utente puo' cambiare budget piu' volte, poi cliccare Esegui o Salta.
    """
    from .executor import execute_signal

    signal_id = signal_row["id"]
    current_budget = float(config.margin_budget_eur)
    sizing = _preview_sizing(proposal, asset_features, current_budget)

    # Drain pre-messaggio per ignorare callback di signal precedenti.
    start_offset = telegram.drain_updates()

    sent = telegram.send_message_with_buttons(
        _format_confirm_message(
            signal_row,
            proposal,
            asset_features,
            current_budget,
            sizing,
            config.confirm_timeout_sec,
        ),
        _confirm_buttons(
            signal_id, current_budget, _min_entry_eur(asset_features)
        ),
    )
    message_id = sent.get("message_id")

    deadline_abs = time.time() + config.confirm_timeout_sec
    while True:
        remaining = int(deadline_abs - time.time())
        if remaining <= 0:
            data = None
            break
        data, _, start_offset = telegram.wait_for_callback(
            valid_prefixes=(
                "budget:",
                f"exec:{signal_id}",
                f"skip:{signal_id}",
            ),
            timeout_sec=remaining,
            start_offset=start_offset,
        )

        if data is None:
            db.update_signal_status(signal_id, "expired")
            log.info("Signal %s scaduto per timeout", signal_id)
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"⌛ <b>Signal {signal_id} scaduto</b>\n"
                    f"Nessuna risposta entro il timeout.",
                )
            return

        parts = data.split(":")
        action = parts[0]

        if action == "budget":
            try:
                new_budget = float(parts[1])
                if int(parts[2]) != signal_id:
                    continue  # callback di altro signal, ignora
            except (IndexError, ValueError):
                continue
            current_budget = new_budget
            sizing = _preview_sizing(proposal, asset_features, current_budget)
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    _format_confirm_message(
                        signal_row,
                        proposal,
                        asset_features,
                        current_budget,
                        sizing,
                        config.confirm_timeout_sec,
                    ),
                )
                # Bottoni vanno re-inviati con la nuova selezione: Telegram
                # editMessageText non ne supporta il refresh diretto, usiamo
                # editMessageReplyMarkup separatamente.
                telegram._post(
                    "/editMessageReplyMarkup",
                    {
                        "chat_id": telegram._chat_id,
                        "message_id": message_id,
                        "reply_markup": {
                            "inline_keyboard": _confirm_buttons(
                                signal_id,
                                current_budget,
                                _min_entry_eur(asset_features),
                            )
                        },
                    },
                )
            continue  # resta in attesa di altre interazioni

        if action == "skip":
            db.update_signal_status(signal_id, "skipped")
            log.info("Signal %s saltato dall'utente", signal_id)
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"❌ <b>Signal {signal_id} saltato</b>",
                )
            return

        if action == "exec":
            if message_id:
                telegram.edit_message_text(
                    message_id,
                    f"⏳ <b>Apertura posizione in corso</b> "
                    f"(signal {signal_id}, budget €{current_budget:.0f})...",
                )
            result = execute_signal(
                config,
                capital,
                db,
                signal_row,
                asset_features,
                margin_budget_override=current_budget,
            )
            telegram.send_message(_format_execution_message(result, proposal))
            return


def run_morning_scan(config: Config) -> None:
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

    log.info("Fetch features per %d asset", len(UNIVERSE))
    features = _collect_features(capital, UNIVERSE)
    if not features:
        telegram.send_message(
            "⚠️ Scanner mattutino: nessun dato di mercato disponibile."
        )
        return

    log.info("Ranking LLM su %d asset", len(features))
    proposals = llm.rank_setups(features)

    eligible = [p for p in proposals if p.direction in ("long", "short")]
    eligible.sort(key=lambda p: p.score, reverse=True)

    top = _pick_top_setup(
        eligible,
        features,
        min_score=config.min_score_threshold,
    )

    if top:
        asset_features = features.get(top.asset, {})
        signal_row = db.insert_signal(
            {
                "asset": top.asset,
                "direction": top.direction,
                "score": top.score,
                "thesis": top.thesis,
                "entry_price": asset_features.get("last_price"),
                # In stop_loss/take_profit memorizziamo le PERCENTUALI
                # suggerite dal LLM rispetto al prezzo di entrata.
                "stop_loss": top.suggested_stop_pct,
                "take_profit": top.suggested_target_pct,
                "size": None,
                "expected_cost": None,
                "status": "pending",
            }
        )
        log.info("Signal salvato id=%s", signal_row.get("id"))

        telegram.send_message(
            _format_telegram_message(
                top, asset_features, execution_mode=config.execution_mode
            )
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
            _handle_confirm(config, capital, db, telegram, signal_row, top, asset_features)
        else:
            log.info("EXECUTION_MODE=coach, solo notifica")
    else:
        telegram.send_message(_format_no_setup_message(eligible))
