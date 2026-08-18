"""Esecutore del grid. Gira ogni 15 minuti.

Sicurezze (denaro reale):
- GRID_ENABLED off di default: senza flag non tocca nulla;
- STOP-LOSS obbligatorio su OGNI apertura (gate del progetto: niente soldi veri
  senza SL fail-closed). Lo stop e' di catastrofe, ancorato a p0, non di trading:
  a fermare il grid prima ci pensa il kill switch sul P&L;
- cap rigido sul numero di unita' aperte;
- kill switch su perdita aggregata e su equity sotto il floor: chiude tutto;
- non apre se il margine libero scende sotto una soglia (il grid CONVIVE con v2
  sullo stesso conto e non deve mai mangiargli il margine);
- opera solo a mercato TRADEABLE;
- lo stato e' derivato dal broker, non da un DB che puo' disallinearsi.

Uso:
  python -m jobs.grid_run --dry-run    # calcola e stampa, non esegue
  python -m jobs.grid_run              # esegue (richiede GRID_ENABLED=true)
  python -m jobs.grid_run --stop       # chiude tutto il grid e si ferma
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config
from src.grid_trader import pianifica, livello

log = logging.getLogger(__name__)
STATE = Path(__file__).resolve().parent.parent / "data" / "grid_state.json"


def _f(name, default):
    return float(os.environ.get(name, str(default)))


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in sys.argv
    stop = "--stop" in sys.argv

    from src.capital_client import CapitalClient
    from src.telegram_client import TelegramClient
    from src.executor import _market_meta

    v1 = load_config()
    enabled = os.environ.get("GRID_ENABLED", "false").strip().lower() == "true"
    epic = os.environ.get("GRID_EPIC", "BTCUSD")
    side = os.environ.get("GRID_SIDE", "long").strip().lower()
    step = _f("GRID_STEP", 0.02)
    max_pos = int(_f("GRID_MAX_POS", 8))
    kill_pnl = _f("GRID_KILL_PNL_EUR", 15.0)
    kill_eq = _f("GRID_KILL_EQUITY_EUR", 40.0)
    catastrofe = _f("GRID_CATASTROPHE", 0.35)
    min_avail = _f("GRID_MIN_AVAILABLE_EUR", 10.0)

    if not enabled and not dry and not stop:
        log.info("GRID_ENABLED=false: skip.")
        return 0

    capital = CapitalClient(v1)
    capital.login()
    telegram = TelegramClient(v1)

    mk = capital.get_market(epic)
    snap = mk.get("snapshot", {}) or {}
    meta = _market_meta(mk, leverages_map=capital.get_leverages_map(),
                        use_real_leverage=True)
    prezzo = meta["mid_price"]
    stato_mercato = snap.get("marketStatus")
    if not prezzo:
        log.error("nessun prezzo per %s", epic)
        return 1

    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    equity = float(bal.get("balance") or 0) + float(bal.get("profitLoss") or 0)
    disponibile = float(bal.get("available") or 0)

    # posizioni del grid = quelle sul suo epic (v2 vive su un altro strumento)
    posizioni = []
    for p in capital.get_open_positions():
        po, m = p.get("position", {}), p.get("market", {})
        if m.get("epic") != epic:
            continue
        if (po.get("direction") or "").upper() != ("BUY" if side == "long" else "SELL"):
            continue
        posizioni.append({
            "deal_id": po.get("dealId"),
            "prezzo_apertura": float(po.get("level") or 0),
            "pnl": float(po.get("upl") or 0),
            "size": float(po.get("size") or 0),
        })

    # ancoraggio della griglia: fissato al primo avvio, mai spostato dopo
    if STATE.exists():
        st = json.loads(STATE.read_text())
        p0 = float(st.get("p0") or prezzo)
    else:
        st = {"p0": prezzo, "epic": epic, "side": side, "step": step,
              "creato": datetime.now(timezone.utc).isoformat()}
        p0 = prezzo
        if not dry:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(st, indent=1))
            log.info("griglia ancorata a p0=%.2f", p0)

    if stop:
        for p in posizioni:
            try:
                capital.close_position(p["deal_id"])
                log.info("chiusa %s", p["deal_id"])
            except Exception as exc:
                log.error("chiusura %s fallita: %s", p["deal_id"], exc)
        if STATE.exists():
            STATE.unlink()
        telegram.send_message(f"⏹️ <b>Grid fermato</b>\n{len(posizioni)} unita' chiuse su {epic}.")
        return 0

    piano = pianifica(prezzo, p0, step, posizioni, side=side, max_posizioni=max_pos,
                      kill_pnl_eur=kill_pnl, equity=equity, kill_equity_eur=kill_eq)

    log.info("%s %.2f | p0 %.2f | gradino %d | aperte %d %s | P&L %.2f€ | %s",
             epic, prezzo, p0, piano.livello_corrente, len(posizioni),
             piano.livelli_aperti, piano.pnl_aperto, piano.motivo)

    if dry:
        print(f"\n=== DRY-RUN grid {epic} {side} ===")
        print(f"  prezzo {prezzo:.2f} | ancoraggio p0 {p0:.2f} | passo {step:.1%}")
        print(f"  gradino corrente: {piano.livello_corrente} | aperti: {piano.livelli_aperti}")
        print(f"  equity {equity:.2f}€ | disponibile {disponibile:.2f}€ | mercato {stato_mercato}")
        print(f"  P&L aperto: {piano.pnl_aperto:+.2f}€")
        for a in piano.azioni:
            print(f"  -> {a.tipo.upper()} liv {a.livello} {a.deal_id or ''}: {a.motivo}")
        if not piano.azioni:
            print("  -> nessuna azione")
        return 0

    if stato_mercato != "TRADEABLE":
        log.info("mercato %s: nessuna azione", stato_mercato)
        return 0

    eseguite = []
    for a in piano.azioni:
        if a.tipo == "chiudi":
            try:
                r = capital.close_position(a.deal_id)
                conf = capital.confirm_deal(r.get("dealReference")) if r.get("dealReference") else {}
                pnl = conf.get("profit")
                eseguite.append(f"chiusa liv {a.livello} ({pnl:+.2f}€)" if pnl is not None
                                else f"chiusa liv {a.livello}")
                log.info("chiusa %s liv %d pnl %s", a.deal_id, a.livello, pnl)
            except Exception as exc:
                log.error("chiusura fallita: %s", exc)
        elif a.tipo == "apri":
            if disponibile < min_avail:
                log.warning("margine libero %.2f€ < %.2f€: NON apro (tutela v2)",
                            disponibile, min_avail)
                continue
            # SL di catastrofe obbligatorio (gate del progetto)
            sl = (p0 * (1 - catastrofe)) if side == "long" else (p0 * (1 + catastrofe))
            try:
                r = capital.create_position(
                    epic, "BUY" if side == "long" else "SELL", meta["min_size"],
                    stop_level=round(sl, 2))
                conf = capital.confirm_deal(r.get("dealReference")) if r.get("dealReference") else {}
                if (conf.get("dealStatus") or "").upper() not in ("ACCEPTED", ""):
                    log.error("apertura rifiutata: %s", conf)
                    continue
                eseguite.append(f"aperta liv {a.livello} @ {conf.get('level') or prezzo}")
                log.info("aperta liv %d @ %s", a.livello, conf.get("level"))
            except Exception as exc:
                log.error("apertura fallita: %s", exc)

    if eseguite:
        killed = piano.motivo.startswith("KILL")
        telegram.send_message(
            f"{'🛑' if killed else '🔲'} <b>Grid {epic} {side}</b>\n"
            f"prezzo {prezzo:.2f} | gradino {piano.livello_corrente} | "
            f"aperte {len(posizioni)}→{len(posizioni) + sum(1 for e in eseguite if e.startswith('aperta')) - sum(1 for e in eseguite if e.startswith('chiusa'))}\n"
            + "\n".join(f"• {_esc(e)}" for e in eseguite)
            + (f"\n<i>{_esc(piano.motivo)}</i>" if killed else "")
        )
        if killed and STATE.exists():
            STATE.unlink()
            log.warning("kill switch: stato rimosso, il grid non riparte da solo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
