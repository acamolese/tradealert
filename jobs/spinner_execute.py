"""Esecutore TradeSpinner (spec §6.3, §11) — SOLO DEMO, auto.

Legge il target_portfolio dell'ultimo scan, apre le posizioni mancanti e chiude
quelle non piu' in target sul conto DEMO, tracciando lo stato in
spinner.executor_position, e notifica ogni mossa su Telegram ([ODDS-EXEC]).

SICUREZZA (apertura ordini = azione non reversibile): gira solo se
  EXECUTION_TARGET=demo  E  capital_env=demo  E  endpoint = demo-api
Il .env globale ha CAPITAL_ENV=live (v1/v2 operano sul conto reale): il cron
dell'esecutore forza CAPITAL_ENV=demo. Se quell'override mancasse, il guard
ABORTISCE invece di rischiare il conto reale (fail-safe).

Nessuno stop-loss: lo spinner e' buy&hold a leva ottimale, l'uscita e' la
re-valutazione giornaliera (esci appena non piu' eligible). Su demo il rischio
di coda e' accettato a f<=1.

Uso: CAPITAL_ENV=demo EXECUTION_TARGET=demo SPINNER_EQUITY_CAP=100 \\
     PYTHONPATH=$PWD .venv/bin/python -m jobs.spinner_execute [--dry-run]
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from src.config import load_config
from src.spinner_config import load_spinner_config, F_MAX_POS

log = logging.getLogger(__name__)


def _mid(snap):
    b, o = snap.get("bid"), snap.get("offer")
    return ((float(b) + float(o)) / 2) if (b and o) else (float(b) if b else None)


def _plan_size(capital, lev, quote_to_ref, epic, side, f_exec, equity):
    """Ricalcola la size col prezzo fresco: n lotti minimi ~ f_exec*equity/nozionale,
    allineati allo step, con f effettiva <= F_MAX_POS. Ritorna (size, mid, meta) o None."""
    from src.executor import _market_meta
    mk = capital.get_market(epic)
    instr = mk.get("instrument", {}) or {}
    mid = _mid(mk.get("snapshot", {}) or {})
    if not mid:
        return None
    q2r = quote_to_ref(instr.get("currency"), capital) or 1.0
    meta = _market_meta(mk, leverages_map=lev, use_real_leverage=True)
    min_size = meta["min_size"]
    step = meta.get("size_step") or min_size
    notional_per_min = min_size * mid * q2r
    if notional_per_min <= 0 or equity <= 0:
        return None
    n = max(1, round(float(f_exec) * equity / notional_per_min))
    size = max(min_size, round(n * min_size / step) * step)
    f_eff = size * mid * q2r / equity
    while f_eff > F_MAX_POS and size - step >= min_size - 1e-9:
        size = round(size - step, 6)
        f_eff = size * mid * q2r / equity
    return round(size, 4), mid, f_eff


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv

    cfg = load_config()
    scfg = load_spinner_config()

    # --- GUARD di sicurezza: mai il conto reale ---
    if scfg.execution_target != "demo":
        log.info("EXECUTION_TARGET=%s (non demo): esecutore no-op.", scfg.execution_target)
        return 0
    if cfg.capital_env != "demo" or "demo-api" not in cfg.capital_base_url:
        log.error("SICUREZZA: esecutore spinner ammesso solo su DEMO. "
                  "capital_env=%s base_url=%s -> ABORT.", cfg.capital_env, cfg.capital_base_url)
        return 2

    from src.capital_client import CapitalClient
    from src.db import Database
    from src.risk import quote_to_ref_factor
    from src.telegram_client import TelegramClient

    db = Database(cfg)
    sp = db._client.schema("spinner")
    capital = CapitalClient(cfg)
    capital.login()
    lev = capital.get_leverages_map()

    equity_raw = float(((capital.get_account_info().get("accounts") or [{}])[0]
                        .get("balance") or {}).get("balance") or 0.0)
    equity = min(equity_raw, scfg.equity_cap) if scfg.equity_cap else equity_raw
    log.info("DEMO equity=%.2f (raw %.2f, cap %s)", equity, equity_raw, scfg.equity_cap)

    # target corrente = righe dell'ultimo as_of_date
    allt = (sp.table("target_portfolio").select("*")
            .order("as_of_date", desc=True).limit(50).execute().data or [])
    if not allt:
        log.info("nessun target_portfolio: niente da fare.")
        return 0
    maxd = allt[0]["as_of_date"]
    target = {(t["epic"], t["side"]): t for t in allt if t["as_of_date"] == maxd}

    # stato spinner: posizioni aperte da noi (closed_at null)
    open_rows = (sp.table("executor_position").select("*")
                 .is_("closed_at", "null").execute().data or [])
    open_by_key = {(r["epic"], r["side"]): r for r in open_rows}

    # posizioni realmente presenti sul demo (per riconciliare i deal_id)
    cap_pos = capital.get_open_positions() or []
    cap_deal_ids = {p.get("position", {}).get("dealId") for p in cap_pos}

    to_close = [(k, r) for k, r in open_by_key.items() if k not in target]
    to_open = [(k, t) for k, t in target.items() if k not in open_by_key]
    holds = [k for k in target if k in open_by_key]

    now = datetime.now(timezone.utc).isoformat()
    moves = []

    # --- CHIUSURE (non piu' in target) ---
    for (epic, side), r in to_close:
        did = r.get("deal_id")
        if did and did not in cap_deal_ids:
            # gia' sparita dal conto (SL manuale/altro): riconcilia lo stato
            if not dry:
                sp.table("executor_position").update(
                    {"closed_at": now, "close_reason": "reconcile_gone"}
                ).eq("id", r["id"]).execute()
            moves.append(f"~ {epic} {side}: gia' chiusa sul demo, stato riconciliato")
            continue
        try:
            if not dry and did:
                res = capital.close_position(did)
                conf = capital.confirm_deal(res.get("dealReference"))
                lvl = conf.get("level")
                sp.table("executor_position").update(
                    {"closed_at": now, "close_price": lvl, "close_reason": "exit_not_target"}
                ).eq("id", r["id"]).execute()
            moves.append(f"- CHIUSA {epic} {side} (non piu' eligible)")
        except Exception as e:
            log.exception("chiusura %s fallita", epic)
            moves.append(f"! errore chiusura {epic} {side}: {e}")

    # --- APERTURE (nuove in target) ---
    for (epic, side), t in to_open:
        try:
            plan = _plan_size(capital, lev, quote_to_ref_factor, epic, side,
                              t["f_exec"], equity)
            if not plan:
                moves.append(f"! {epic} {side}: prezzo/size non calcolabile, skip")
                continue
            size, mid, f_eff = plan
            direction = "BUY" if side == "long" else "SELL"
            if dry:
                moves.append(f"+ (dry) APRIREI {epic} {side} size {size} f~{f_eff:.2f} @ {mid}")
                continue
            res = capital.create_position(epic, direction, size)  # niente SL: buy&hold
            conf = capital.confirm_deal(res.get("dealReference"))
            if conf.get("dealStatus") != "ACCEPTED":
                moves.append(f"! {epic} {side} RIFIUTATA: {conf.get('reason') or conf.get('dealStatus')}")
                continue
            deal_id = conf.get("dealId")
            lvl = conf.get("level") or mid
            sp.table("executor_position").insert({
                "epic": epic, "side": side, "deal_id": deal_id,
                "deal_reference": res.get("dealReference"), "size": size,
                "f_exec": t["f_exec"], "open_price": lvl,
            }).execute()
            moves.append(f"+ APERTA {epic} {side} size {size} f~{f_eff:.2f} @ {lvl}")
        except Exception as e:
            log.exception("apertura %s fallita", epic)
            moves.append(f"! errore apertura {epic} {side}: {e}")

    # --- notifica ---
    head = (f"[ODDS-EXEC] {maxd} (DEMO, equity {equity:.0f})\n"
            f"target {len(target)} | tenute {len(holds)} | "
            f"aperte {sum(1 for m in moves if m.startswith('+ APERTA'))} | "
            f"chiuse {sum(1 for m in moves if m.startswith('- CHIUSA'))}")
    body = "\n".join(moves) if moves else " (nessuna mossa: portafoglio gia' allineato)"
    msg = head + "\n" + body
    print(msg)
    if not dry:
        esc = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            TelegramClient(cfg).send_message("<pre>" + esc + "</pre>")
        except Exception:
            log.exception("invio Telegram fallito")
    return 0


if __name__ == "__main__":
    sys.exit(main())
