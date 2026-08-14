"""Job del controller di esposizione v2 (spec §4). Gira 1x/giorno dopo la chiusura.

Orchestrazione: legge da Capital (candele DAY, equity, leva reale, taglia minima),
calcola il piano con src/exposure_controller.plan_exposure (puro), scrive
exposure_state, e — solo se abilitato e non dry-run — apre/chiude blocchi con
finestra di veto su Telegram, registrandoli in `block`.

Uso:
  PYTHONPATH=$PWD .venv/bin/python -m jobs.exposure_controller --dry-run   # calcola, non esegue
  PYTHONPATH=$PWD .venv/bin/python -m jobs.exposure_controller             # reale (richiede V2_EXPOSURE_ENABLED)
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from src.config import load_config
from src.exposure_config import load_exposure_config
from src.exposure_controller import plan_exposure
from src.instrument_selector import SwitchDecision
from src.volatility import ewma_sigma
from src.risk import calculate_size, quote_to_ref_factor

log = logging.getLogger(__name__)


def _esc(text: object) -> str:
    """Escape HTML per Telegram (i reason possono contenere '<', es. '0 < correnti')."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _mid_closes(prices: list[dict]) -> list[float]:
    out: list[float] = []
    for p in prices:
        cp = p.get("closePrice") or {}
        bid, ask = cp.get("bid"), cp.get("ask")
        if bid is not None and ask is not None:
            out.append((float(bid) + float(ask)) / 2.0)
        elif bid is not None:
            out.append(float(bid))
    return out


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _instrument_ctx(capital, epic: str, cfg):
    """Contesto di mercato di uno strumento: (meta, mid, L, q2r, min_margin).
    Solleva ValueError se manca il prezzo. min_margin e' il costo in EUR della
    taglia minima del broker: se supera il blocco, lo strumento non e' eseguibile
    da v2 (§2.1)."""
    from src.executor import _market_meta
    from src.leverage import real_leverage

    market = capital.get_market(epic)
    instr = market.get("instrument", {}) or {}
    meta = _market_meta(market, leverages_map=capital.get_leverages_map(),
                        use_real_leverage=True)
    mid = meta["mid_price"]
    if not mid:
        raise ValueError(f"nessun prezzo per {epic}")
    L = real_leverage(epic) or (1.0 / meta["margin_factor"] if meta["margin_factor"] else 20.0)
    q2r = quote_to_ref_factor(instr.get("currency"), capital) or 1.0
    min_margin = meta["min_size"] * mid * meta["margin_factor"] * q2r
    return meta, mid, L, q2r, min_margin


def _pick_instrument(capital, db, cfg, epic_current: str, plan_action: str, today: str):
    """Valuta la sostituzione dello strumento leggendo il tabellone dello spinner.
    Ritorna (SwitchDecision, best_epic|None). Non esegue nulla.

    Il tabellone gira su DEMO: se ne prende solo la MISURA (net_adj, sigma), mentre
    l'eseguibilita' e' verificata sul conto REALE, strumento per strumento.
    """
    from src.instrument_selector import rank_candidates, decide_switch

    def _no(reason: str):
        return SwitchDecision(False, epic_current, None, reason), None

    try:
        sp = db._client.schema("spinner")
        last = (sp.table("odds_board").select("scan_date")
                .order("scan_date", desc=True).limit(1).execute().data)
        if not last:
            return _no("tabellone assente: nessuno switch")
        scan_date = last[0]["scan_date"]
        age = (datetime.fromisoformat(today).date()
               - datetime.fromisoformat(scan_date).date()).days
        rows = (sp.table("odds_board")
                .select("epic,side,net_adj,sigma_ann,asset_class")
                .eq("scan_date", scan_date).eq("status", "eligible").execute().data)
    except Exception:
        log.exception("lettura tabellone fallita: nessuno switch (fail-closed)")
        return _no("lettura tabellone fallita: nessuno switch")

    # pre-ranking sui soli dati del tabellone (nessuna chiamata di rete), poi
    # verifica di eseguibilita' sul reale solo per i primi K + quello detenuto.
    pre = rank_candidates(rows, {r["epic"] for r in rows if r.get("epic")})
    shortlist = [c.epic for c in pre[:5]]
    if epic_current not in shortlist:
        shortlist.append(epic_current)
    executable: set[str] = set()
    for ep in shortlist:
        try:
            _, _, _, _, min_margin = _instrument_ctx(capital, ep, cfg)
        except Exception:
            log.warning("eseguibilita' di %s non verificabile: escluso", ep)
            continue
        if min_margin <= cfg.block_margin_eur:
            executable.add(ep)
        else:
            log.info("%s non eseguibile: taglia minima %.2f€ > blocco %.0f€",
                     ep, min_margin, cfg.block_margin_eur)

    candidates = rank_candidates(rows, executable)
    best = candidates[0].epic if candidates else None

    # storia dei pick per l'isteresi di switch (giorni PRECEDENTI, piu' recente ultimo)
    try:
        prev = (db._client.table("monitoring_events").select("details,created_at")
                .eq("event_type", "v2_instrument_pick").lt("created_at", today)
                .order("created_at", desc=True)
                .limit(cfg.switch_stable_days + 2).execute().data)
        picks = [(p.get("details") or {}).get("best_epic") for p in reversed(prev)]
        picks = [p for p in picks if p]
    except Exception:
        log.exception("lettura storico pick fallita: isteresi non verificabile")
        return _no("storico pick illeggibile: nessuno switch")

    decision = decide_switch(
        epic_current, candidates, picks,
        plan_action=plan_action, board_age_days=age,
        enabled=cfg.switch_enabled, min_edge=cfg.switch_min_edge,
        stable_days=cfg.switch_stable_days,
        board_max_age_days=cfg.board_max_age_days,
    )

    # registra il pick del giorno (serve all'isteresi di domani anche a flag off,
    # cosi' l'attivazione parte con una storia gia' pronta)
    try:
        db._client.table("monitoring_events").insert({
            "event_type": "v2_instrument_pick",
            "details": {"best_epic": best, "current_epic": epic_current,
                        "scan_date": scan_date, "board_age_days": age,
                        "edge": round(decision.edge, 4) if decision.edge != float("inf") else None,
                        "switch": decision.switch, "reason": decision.reason,
                        "ranking": [{"epic": c.epic, "score": round(c.score, 4)}
                                    for c in candidates[:5]]},
        }).execute()
    except Exception:
        log.exception("log v2_instrument_pick fallito (proseguo)")

    return decision, best


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv

    from src.capital_client import CapitalClient
    from src.db import Database
    from src.telegram_client import TelegramClient

    v1 = load_config()
    cfg = load_exposure_config()
    db = Database(v1)
    telegram = TelegramClient(v1)

    if not cfg.enabled and not dry:
        log.info("V2_EXPOSURE_ENABLED=false e non dry-run: skip.")
        return 0

    capital = CapitalClient(v1)
    capital.login()

    # --- strumento corrente: lo dettano i blocchi APERTI, non la config (2026-08-14).
    # Se l'epic cambia, i blocchi restano dov'erano finche' non li si chiude: leggerli
    # tutti evita di renderli invisibili al controller cambiando V2_EPIC. ---
    today = _today()
    all_open = (db._client.table("block").select("id,deal_id,epic,opened_at")
                .is_("closed_at", "null").order("opened_at").execute().data)
    epics_open = {b["epic"] for b in all_open}
    if len(epics_open) > 1:
        telegram.send_message(
            f"🛑 <b>v2 incoerente</b>\nBlocchi aperti su piu' strumenti: "
            f"{', '.join(sorted(epics_open))}. Il controller gestisce un solo "
            f"strumento per volta: intervento manuale richiesto."
        )
        log.error("Blocchi aperti su piu' epic: %s. Abort.", epics_open)
        return 1
    epic = epics_open.pop() if epics_open else cfg.epic
    open_blocks = [b for b in all_open if b["epic"] == epic]

    # --- mercato, leva reale, vincolo taglia minima (§2.1) ---
    try:
        meta, mid, L, q2r, min_margin = _instrument_ctx(capital, epic, cfg)
    except Exception as exc:
        log.error("Contesto di mercato per %s non disponibile: %s", epic, exc)
        return 1
    if min_margin > cfg.block_margin_eur:
        telegram.send_message(
            f"🛑 <b>v2 non avviabile</b>\n{epic}: la taglia minima broker richiede "
            f"~{min_margin:.2f}€ di margine &gt; blocco {cfg.block_margin_eur:.0f}€. "
            f"Un blocco non e' eseguibile alla taglia minima (§2.1)."
        )
        log.error("Taglia minima %.2f€ > blocco %.0f€: refuse.", min_margin, cfg.block_margin_eur)
        return 1

    # --- equity ---
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    # §2.2 "E = equity del conto": saldo realizzato + P&L flottante delle posizioni
    # aperte. Cosi' il guadagno flottante fa crescere N_max e il compounding e'
    # automatico (non serve chiudere per aumentare la capacita' di esposizione).
    _bal = acc.get("balance") or {}
    equity = float(_bal.get("balance") or 0.0) + float(_bal.get("profitLoss") or 0.0)

    # --- volatilita' da candele giornaliere ---
    prices = capital.get_prices(epic, resolution="DAY", max_bars=400)
    closes = _mid_closes(prices)
    sigma = ewma_sigma(closes)

    # --- stato corrente: blocchi aperti + storico target ---
    blocks_current = len(open_blocks)
    # isteresi sui giorni PRECEDENTI: escludi la riga di oggi, altrimenti run
    # ripetuti nello stesso giorno soddisferebbero l'isteresi falsamente.
    # Filtrata per epic: dopo uno switch la storia riparte, quindi gli AUMENTI
    # attendono di nuovo l'isteresi mentre le riduzioni restano immediate.
    hist = (db._client.table("exposure_state").select("blocks_target,as_of_date")
            .eq("epic", epic).lt("as_of_date", today)
            .order("as_of_date", desc=True).limit(cfg.hysteresis_days + 2).execute().data)
    recent_targets = [int(r["blocks_target"]) for r in reversed(hist)]

    # --- macro impact (§5.1): letto dall'ultimo macro_variance di oggi. Con
    # MACRO_SCALE_ENABLED off (v1) si LOGGA ma NON si applica (macro_scale resta 1.0). ---
    MACRO_MAP = {"low": 1.0, "medium": 0.7, "high": 0.5}
    macro_impact = None
    try:
        mv = (db._client.table("monitoring_events").select("details,created_at")
              .eq("event_type", "macro_variance").gte("created_at", today)
              .order("created_at", desc=True).limit(1).execute().data)
        if mv:
            macro_impact = (mv[0].get("details") or {}).get("impact")
    except Exception:
        log.exception("lettura macro_variance fallita (proseguo)")
    macro_scale = MACRO_MAP.get(macro_impact, 1.0) if cfg.macro_scale_enabled else 1.0

    plan = plan_exposure(sigma, blocks_current, recent_targets, equity, L, cfg, macro_scale)

    log.info("Piano: sigma=%s scale=%.3f n_max=%d cur=%d tgt=%d reach=%d delta=%d action=%s | %s",
             f"{sigma:.4f}" if sigma else "None", plan.scale_applied, plan.n_max,
             blocks_current, plan.blocks_target, plan.blocks_to_reach, plan.delta, plan.action, plan.reason)

    # --- scrivi exposure_state (idempotente sul giorno) ---
    state_row = {
        "as_of_date": today, "epic": epic,
        "sigma_hat": round(sigma, 6) if sigma else 0.0,
        "scale_raw": round(plan.scale_raw, 6), "scale_applied": round(plan.scale_applied, 6),
        "macro_impact": macro_impact, "macro_scale": macro_scale,
        "n_max": plan.n_max, "blocks_target": plan.blocks_target,
        "blocks_current": blocks_current, "action": plan.action, "delta": plan.delta,
        "equity_eur": round(equity, 2), "real_leverage": L,
    }
    try:
        db._client.table("exposure_state").upsert(state_row, on_conflict="as_of_date").execute()
    except Exception:
        log.exception("upsert exposure_state fallito (proseguo)")

    # --- selezione dello strumento (2026-08-14): si valuta SEMPRE (anche a flag
    # off e in dry-run) per costruire la storia dei pick, ma esegue solo con
    # V2_SWITCH_ENABLED e piano 'hold'. ---
    decision, best = _pick_instrument(capital, db, cfg, epic, plan.action, today)
    log.info("Strumento: corrente=%s migliore=%s switch=%s | %s",
             epic, best, decision.switch, decision.reason)

    if dry:
        print("\n=== DRY-RUN — nessun ordine ===")
        for k, v in state_row.items():
            print(f"  {k}: {v}")
        print(f"  motivo: {plan.reason}")
        print(f"  strumento migliore: {best} | switch: {decision.switch}")
        print(f"  motivo switch: {decision.reason}")
        return 0

    if decision.switch and decision.to_epic:
        return _switch_instrument(capital, db, telegram, cfg, epic, decision,
                                  open_blocks, blocks_current, sigma)

    if plan.delta == 0:
        log.info("delta 0: nessuna azione.")
        return 0

    # --- proposta su Telegram (§4.3) ---
    verb = "APRE" if plan.delta > 0 else ("HALT/chiude" if plan.action == "halt" else "chiude")
    proposta = "" if cfg.auto_execute else "\n<i>(V2_AUTO_EXECUTE off: proposta, non eseguita)</i>"
    telegram.send_message(
        f"📦 <b>Controller esposizione — {epic}</b>\n"
        f"Blocchi: {blocks_current} → <b>{plan.blocks_to_reach}</b> (target {plan.blocks_target}, N_max {plan.n_max})\n"
        f"σ stimata: {sigma:.1%} | scale {plan.scale_applied:.2f}\n"
        f"Azione: <b>{verb} {abs(plan.delta)}</b>\n<i>{_esc(plan.reason)}</i>{proposta}"
    )

    # Esecuzione fisica gated dal flag auto_execute finche' il veto pieno (§4.3) non
    # e' implementato: le RIDUZIONI di rischio si eseguono comunque (halt/close),
    # gli AUMENTI (apertura) solo con auto_execute on.
    if not cfg.auto_execute and plan.delta > 0:
        log.info("auto_execute off: apertura solo proposta, non eseguita.")
        return 0

    if plan.delta > 0:
        _open_blocks(capital, db, telegram, cfg, epic, meta, mid, q2r, plan.delta, state_row)
    else:
        _close_blocks(capital, db, telegram, cfg, epic, open_blocks, abs(plan.delta),
                      "kill_switch" if plan.action == "halt" else "controller")
    return 0


def _switch_instrument(capital, db, telegram, cfg, epic_from, decision,
                       open_blocks, blocks_current, sigma):
    """Sostituisce lo strumento a ESPOSIZIONE INVARIATA: chiude n blocchi su A e
    ne apre n su B. Non e' un aumento di rischio, quindi non passa dall'isteresi
    sui blocchi (che protegge dal churn sul LIVELLO di esposizione, non sulla
    scelta dello strumento).

    Fail-safe: se l'apertura su B fallisce dopo la chiusura di A, il sistema resta
    FLAT e lo dichiara. Flat e' lo stato sicuro: il giorno dopo il controller
    ricostruisce l'esposizione dal piano normale.
    """
    epic_to = decision.to_epic
    try:
        meta_b, mid_b, _L_b, q2r_b, min_margin_b = _instrument_ctx(capital, epic_to, cfg)
    except Exception as exc:
        log.error("Contesto di %s non disponibile: switch annullato (%s)", epic_to, exc)
        return 1
    if min_margin_b > cfg.block_margin_eur:
        log.error("%s non piu' eseguibile (%.2f€ > %.0f€): switch annullato",
                  epic_to, min_margin_b, cfg.block_margin_eur)
        return 1

    telegram.send_message(
        f"🔄 <b>Cambio strumento</b>\n{epic_from} → <b>{epic_to}</b>\n"
        f"Blocchi: {blocks_current} (esposizione invariata)\n"
        f"<i>{_esc(decision.reason)}</i>"
    )

    if blocks_current:
        _close_blocks(capital, db, telegram, cfg, epic_from, open_blocks,
                      blocks_current, "instrument_switch")
        residui = (db._client.table("block").select("id")
                   .eq("epic", epic_from).is_("closed_at", "null").execute().data)
        if residui:
            telegram.send_message(
                f"🛑 <b>Switch interrotto</b>\n{len(residui)} blocco/i su {epic_from} "
                f"non chiuso/i: NON apro su {epic_to} per non restare esposto su due "
                f"strumenti. Intervento manuale."
            )
            log.error("Chiusura incompleta su %s: %d residui. Abort switch.",
                      epic_from, len(residui))
            return 1

    if blocks_current:
        _open_blocks(capital, db, telegram, cfg, epic_to, meta_b, mid_b, q2r_b,
                     blocks_current, {})
    log.info("Switch completato: %s -> %s (%d blocchi)", epic_from, epic_to, blocks_current)
    return 0


def _open_blocks(capital, db, telegram, cfg, epic, meta, mid, q2r, n, state_row):
    for _ in range(n):
        sz = calculate_size(
            margin_budget=cfg.block_margin_eur, entry_price=mid,
            margin_factor=meta["margin_factor"], min_size=meta["min_size"],
            size_step=meta["size_step"], stop_pct=cfg.catastrophe_stop_pct * 100.0,
            max_loss_per_trade_eur=None, quote_to_ref=q2r)
        if sz.size is None:
            telegram.send_message(f"⚠️ Apertura blocco fallita: {_esc(sz.reason)}")
            return
        cat = round(mid * (1.0 - cfg.catastrophe_stop_pct), 2)
        try:
            resp = capital.create_position(epic, "BUY", sz.size,
                                           stop_level=cat, guaranteed_stop=cfg.use_guaranteed_stop)
            ref = resp.get("dealReference")
            conf = capital.confirm_deal(ref) if ref else {}
            deal_id = (conf.get("affectedDeals") or [{}])[0].get("dealId") or conf.get("dealId")
            fill = float(conf.get("level") or mid)
        except Exception as exc:
            log.exception("create_position blocco fallita")
            telegram.send_message(f"⚠️ Apertura blocco fallita: {_esc(exc)}")
            return
        db._client.table("block").insert({
            "epic": epic, "deal_id": deal_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "open_price": fill, "size_units": sz.size,
            "margin_eur": round(sz.margin_estimate, 2), "catastrophe_stop": cat,
        }).execute()
        # §6.1 execution_quality: slippage modeled(mid) vs actual(fill)
        try:
            db._client.table("execution_quality").insert({
                "deal_id": deal_id, "side": "open",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "modeled_price": round(mid, 4), "actual_price": round(fill, 4),
                "slippage_bps": round((fill - mid) / mid * 10000, 2) if mid else 0.0,
            }).execute()
        except Exception:
            log.exception("log execution_quality (open) fallito")
        log.info("Blocco aperto: deal=%s fill=%.2f size=%s", deal_id, fill, sz.size)
    telegram.send_message(f"✅ {n} blocco/i aperto/i su {epic}.")


def _close_blocks(capital, db, telegram, cfg, epic, open_blocks, n, reason):
    for b in open_blocks[:n]:  # FIFO: il piu' vecchio prima
        deal_id = b["deal_id"]
        try:
            resp = capital.close_position(deal_id)
            ref = resp.get("dealReference")
            conf = capital.confirm_deal(ref) if ref else {}
            close_px = float(conf.get("level") or 0)
            pnl = conf.get("profit") or conf.get("profitAndLoss")
        except Exception as exc:
            log.exception("close_position blocco fallita")
            telegram.send_message(f"⚠️ Chiusura blocco {deal_id} fallita: {_esc(exc)}")
            continue
        db._client.table("block").update({
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "close_price": close_px or None, "close_reason": reason,
            "pnl_ccy": float(pnl) if pnl is not None else None,
        }).eq("deal_id", deal_id).execute()
        log.info("Blocco chiuso: deal=%s px=%.2f reason=%s", deal_id, close_px, reason)
    telegram.send_message(f"✅ {n} blocco/i chiuso/i su {epic} ({reason}).")


if __name__ == "__main__":
    sys.exit(main())
