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
from src.volatility import ewma_sigma
from src.risk import calculate_size, quote_to_ref_factor

log = logging.getLogger(__name__)


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


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv

    from src.capital_client import CapitalClient
    from src.db import Database
    from src.telegram_client import TelegramClient
    from src.executor import _market_meta
    from src.leverage import real_leverage

    v1 = load_config()
    cfg = load_exposure_config()
    db = Database(v1)
    telegram = TelegramClient(v1)

    if not cfg.enabled and not dry:
        log.info("V2_EXPOSURE_ENABLED=false e non dry-run: skip.")
        return 0

    capital = CapitalClient(v1)
    capital.login()

    # --- mercato, leva reale, vincolo taglia minima (§2.1) ---
    market = capital.get_market(cfg.epic)
    instr = market.get("instrument", {}) or {}
    lev_map = capital.get_leverages_map()
    meta = _market_meta(market, leverages_map=lev_map, use_real_leverage=True)
    L = real_leverage(cfg.epic) or (1.0 / meta["margin_factor"] if meta["margin_factor"] else 20.0)
    q2r = quote_to_ref_factor(instr.get("currency"), capital) or 1.0
    mid = meta["mid_price"]
    if not mid:
        log.error("Nessun prezzo per %s: abort.", cfg.epic)
        return 1
    min_margin = meta["min_size"] * mid * meta["margin_factor"] * q2r
    if min_margin > cfg.block_margin_eur:
        telegram.send_message(
            f"🛑 <b>v2 non avviabile</b>\n{cfg.epic}: la taglia minima broker richiede "
            f"~{min_margin:.2f}€ di margine &gt; blocco {cfg.block_margin_eur:.0f}€. "
            f"Un blocco non e' eseguibile alla taglia minima (§2.1)."
        )
        log.error("Taglia minima %.2f€ > blocco %.0f€: refuse.", min_margin, cfg.block_margin_eur)
        return 1

    # --- equity ---
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    equity = float((acc.get("balance") or {}).get("balance") or 0.0)

    # --- volatilita' da candele giornaliere ---
    prices = capital.get_prices(cfg.epic, resolution="DAY", max_bars=400)
    closes = _mid_closes(prices)
    sigma = ewma_sigma(closes)

    # --- stato corrente: blocchi aperti + storico target ---
    today = _today()
    open_blocks = (db._client.table("block").select("id,deal_id,opened_at")
                   .eq("epic", cfg.epic).is_("closed_at", "null").order("opened_at").execute().data)
    blocks_current = len(open_blocks)
    # isteresi sui giorni PRECEDENTI: escludi la riga di oggi, altrimenti run
    # ripetuti nello stesso giorno soddisferebbero l'isteresi falsamente.
    hist = (db._client.table("exposure_state").select("blocks_target,as_of_date")
            .eq("epic", cfg.epic).lt("as_of_date", today)
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
        "as_of_date": today, "epic": cfg.epic,
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

    if dry:
        print("\n=== DRY-RUN — nessun ordine ===")
        for k, v in state_row.items():
            print(f"  {k}: {v}")
        print(f"  motivo: {plan.reason}")
        return 0

    if plan.delta == 0:
        log.info("delta 0: nessuna azione.")
        return 0

    # --- proposta su Telegram (§4.3) ---
    verb = "APRE" if plan.delta > 0 else ("HALT/chiude" if plan.action == "halt" else "chiude")
    proposta = "" if cfg.auto_execute else "\n<i>(V2_AUTO_EXECUTE off: proposta, non eseguita)</i>"
    telegram.send_message(
        f"📦 <b>Controller esposizione — {cfg.epic}</b>\n"
        f"Blocchi: {blocks_current} → <b>{plan.blocks_to_reach}</b> (target {plan.blocks_target}, N_max {plan.n_max})\n"
        f"σ stimata: {sigma:.1%} | scale {plan.scale_applied:.2f}\n"
        f"Azione: <b>{verb} {abs(plan.delta)}</b>\n<i>{plan.reason}</i>{proposta}"
    )

    # Esecuzione fisica gated dal flag auto_execute finche' il veto pieno (§4.3) non
    # e' implementato: le RIDUZIONI di rischio si eseguono comunque (halt/close),
    # gli AUMENTI (apertura) solo con auto_execute on.
    if not cfg.auto_execute and plan.delta > 0:
        log.info("auto_execute off: apertura solo proposta, non eseguita.")
        return 0

    if plan.delta > 0:
        _open_blocks(capital, db, telegram, cfg, meta, mid, q2r, plan.delta, state_row)
    else:
        _close_blocks(capital, db, telegram, cfg, open_blocks, abs(plan.delta),
                      "kill_switch" if plan.action == "halt" else "controller")
    return 0


def _open_blocks(capital, db, telegram, cfg, meta, mid, q2r, n, state_row):
    from src.executor import _market_meta  # noqa
    for _ in range(n):
        sz = calculate_size(
            margin_budget=cfg.block_margin_eur, entry_price=mid,
            margin_factor=meta["margin_factor"], min_size=meta["min_size"],
            size_step=meta["size_step"], stop_pct=cfg.catastrophe_stop_pct * 100.0,
            max_loss_per_trade_eur=None, quote_to_ref=q2r)
        if sz.size is None:
            telegram.send_message(f"⚠️ Apertura blocco fallita: {sz.reason}")
            return
        cat = round(mid * (1.0 - cfg.catastrophe_stop_pct), 2)
        try:
            resp = capital.create_position(cfg.epic, "BUY", sz.size,
                                           stop_level=cat, guaranteed_stop=cfg.use_guaranteed_stop)
            ref = resp.get("dealReference")
            conf = capital.confirm_deal(ref) if ref else {}
            deal_id = (conf.get("affectedDeals") or [{}])[0].get("dealId") or conf.get("dealId")
            fill = float(conf.get("level") or mid)
        except Exception as exc:
            log.exception("create_position blocco fallita")
            telegram.send_message(f"⚠️ Apertura blocco fallita: {exc}")
            return
        db._client.table("block").insert({
            "epic": cfg.epic, "deal_id": deal_id,
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
    telegram.send_message(f"✅ {n} blocco/i aperto/i su {cfg.epic}.")


def _close_blocks(capital, db, telegram, cfg, open_blocks, n, reason):
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
            telegram.send_message(f"⚠️ Chiusura blocco {deal_id} fallita: {exc}")
            continue
        db._client.table("block").update({
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "close_price": close_px or None, "close_reason": reason,
            "pnl_ccy": float(pnl) if pnl is not None else None,
        }).eq("deal_id", deal_id).execute()
        log.info("Blocco chiuso: deal=%s px=%.2f reason=%s", deal_id, close_px, reason)
    telegram.send_message(f"✅ {n} blocco/i chiuso/i su {cfg.epic} ({reason}).")


if __name__ == "__main__":
    sys.exit(main())
