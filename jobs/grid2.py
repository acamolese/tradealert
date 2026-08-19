"""Grid bidirezionale a posizione netta. Un ordine per run.

Differenze dal precedente jobs/grid_run.py:
- sfrutta il netting: una sola posizione che oscilla tra long e short, quindi
  guadagna sia sui ribassi sia sui rialzi (prima meta' dei movimenti era persa);
- lo stato e' la posizione netta del broker, niente da riconciliare;
- messaggi Telegram leggibili (richiesta 2026-08-19).

Sicurezze: cap sulle unita', kill switch su perdita e su equity, stop-loss sulla
posizione, niente ordini a mercato chiuso, flag per profilo.

Uso:
  python -m jobs.grid2 --profile ORO --dry-run
  python -m jobs.grid2 --profile ORO
  python -m jobs.grid2 --profile ORO --stop
  python -m jobs.grid2 --profile ORO --riparti   # sblocca dopo il traguardo
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config
from src.grid_net import pianifica_net, livello
from src.grid_profit import valuta

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"
PROFILO = ""


def _env(name: str, default: str) -> str:
    if PROFILO:
        v = os.environ.get(f"G2_{PROFILO}_{name}")
        if v is not None:
            return v
    return os.environ.get(f"G2_{name}", default)


def _f(name, default):
    return float(_env(name, str(default)))


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    global PROFILO
    if "--profile" in sys.argv:
        PROFILO = sys.argv[sys.argv.index("--profile") + 1].upper()
    dry = "--dry-run" in sys.argv
    stop = "--stop" in sys.argv
    riparti = "--riparti" in sys.argv

    from src.capital_client import CapitalClient
    from src.telegram_client import TelegramClient
    from src.executor import _market_meta

    v1 = load_config()
    enabled = _env("ENABLED", "false").strip().lower() == "true"
    epic = _env("EPIC", "GOLD")
    step = _f("STEP", 0.002)
    max_unita = int(_f("MAX_UNITA", 10))
    budget = _f("BUDGET_EUR", 0.0)
    kill_pnl = _f("KILL_PNL_EUR", 15.0)
    kill_eq = _f("KILL_EQUITY_EUR", 30.0)
    catastrofe = _f("CATASTROPHE", 0.20)
    # soglie di PROFITTO: avviso e blocco alla crescita del conto (2026-08-19)
    profit_alert = _f("PROFIT_ALERT_EUR", 10.0)
    profit_stop = _f("PROFIT_STOP_EUR", 20.0)

    if not enabled and not dry and not stop:
        log.info("profilo %s disabilitato", PROFILO or "(default)")
        return 0

    capital = CapitalClient(v1)
    capital.login()
    telegram = TelegramClient(v1)

    if riparti:
        # sblocca dopo un blocco per obiettivo raggiunto e riparte dal valore
        # attuale del conto: il guadagno incassato diventa la nuova base.
        acc0 = (capital.get_account_info().get("accounts") or [{}])[0]
        b0 = acc0.get("balance") or {}
        eq0 = float(b0.get("balance") or 0) + float(b0.get("profitLoss") or 0)
        PS = DATA / f"g2_profit_{v1.capital_env}.json"
        PS.write_text(json.dumps({"baseline": round(eq0, 2), "avvisate": [],
                                  "bloccato": False,
                                  "creato": datetime.now(timezone.utc).isoformat()}, indent=1))
        telegram.send_message(
            f"▶️ <b>Grid ripartiti</b>\nNuova base: {eq0:.2f}€\n"
            f"Prossimo avviso a +{_f('PROFIT_ALERT_EUR', 10.0):.0f}€, "
            f"pausa a +{_f('PROFIT_STOP_EUR', 20.0):.0f}€.")
        print(f"ripartito da baseline {eq0:.2f}€")
        return 0

    mk = capital.get_market(epic)
    snap = mk.get("snapshot", {}) or {}
    meta = _market_meta(mk, leverages_map=capital.get_leverages_map(),
                        use_real_leverage=True)
    prezzo = meta["mid_price"]
    if not prezzo:
        log.error("nessun prezzo per %s", epic)
        return 1

    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    equity = float(bal.get("balance") or 0) + float(bal.get("profitLoss") or 0)

    # taglia di una unita' dal budget (margine), altrimenti la minima del broker
    from src.risk import quote_to_ref_factor
    q2r = quote_to_ref_factor((mk.get("instrument") or {}).get("currency"), capital) or 1.0
    marg_min = meta["min_size"] * prezzo * meta["margin_factor"] * q2r
    unit_size = meta["min_size"]
    if budget > 0 and max_unita > 0 and marg_min > 0:
        n = int((budget / max_unita) / marg_min)
        unit_size = round(max(meta["min_size"], n * meta["min_size"]), 8)

    # posizione NETTA sul broker (long positiva, short negativa)
    netta = 0.0
    pnl = 0.0
    deals = []
    for p in capital.get_open_positions():
        po, m = p.get("position", {}), p.get("market", {})
        if m.get("epic") != epic:
            continue
        s = float(po.get("size") or 0)
        if (po.get("direction") or "").upper() == "SELL":
            s = -s
        netta += s
        pnl += float(po.get("upl") or 0)
        deals.append(po.get("dealId"))
    unita_correnti = netta / unit_size if unit_size else 0.0

    STATE = DATA / f"g2_{v1.capital_env}_{epic}.json"
    if STATE.exists():
        p0 = float(json.loads(STATE.read_text()).get("p0") or prezzo)
    else:
        p0 = prezzo
        if not dry and not stop:
            DATA.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"p0": p0, "epic": epic, "step": step,
                                         "creato": datetime.now(timezone.utc).isoformat()}))
            log.info("ancoraggio %s a %.4f", epic, p0)

    if stop:
        for d in deals:
            try:
                capital.close_position(d)
            except Exception as exc:
                log.error("chiusura %s fallita: %s", d, exc)
        if STATE.exists():
            STATE.unlink()
        telegram.send_message(f"⏹️ <b>Grid {epic} fermato</b> ({len(deals)} posizioni chiuse)")
        return 0

    # --- soglie di profitto, stato condiviso da tutti i grid dello STESSO conto ---
    PSTATE = DATA / f"g2_profit_{v1.capital_env}.json"
    ps = {}
    if PSTATE.exists():
        try:
            ps = json.loads(PSTATE.read_text())
        except Exception:
            ps = {}
    if not ps.get("baseline"):
        ps = {"baseline": round(equity, 2), "avvisate": [], "bloccato": False,
              "creato": datetime.now(timezone.utc).isoformat()}
        if not dry:
            DATA.mkdir(parents=True, exist_ok=True)
            PSTATE.write_text(json.dumps(ps, indent=1))
        log.info("baseline profitto fissata a %.2f€", ps["baseline"])

    v = valuta(equity, float(ps["baseline"]), [profit_alert], profit_stop,
               [float(x) for x in ps.get("avvisate", [])], bool(ps.get("bloccato")))

    if v.stato == "gia_bloccato":
        log.info("%s: sistema in pausa (obiettivo raggiunto), nessuna operazione", epic)
        return 0

    if v.stato == "avviso" and not dry:
        ps.setdefault("avvisate", []).append(v.soglia_colpita)
        PSTATE.write_text(json.dumps(ps, indent=1))
        telegram.send_message(
            f"🔔 <b>Conto cresciuto di {v.guadagno:+.2f}€</b>\n"
            f"da {v.baseline:.2f}€ a {equity:.2f}€\n"
            f"Il sistema continua a lavorare. Al prossimo traguardo "
            f"(+{profit_stop:.0f}€) si ferma da solo per farti decidere.")

    if v.stato == "blocco" and not dry:
        chiuse = 0
        for d in deals:
            try:
                capital.close_position(d)
                chiuse += 1
            except Exception as exc:
                log.error("chiusura %s fallita: %s", d, exc)
        ps["bloccato"] = True
        ps["bloccato_a"] = round(equity, 2)
        ps["bloccato_il"] = datetime.now(timezone.utc).isoformat()
        PSTATE.write_text(json.dumps(ps, indent=1))
        telegram.send_message(
            f"🎯 <b>Obiettivo raggiunto: {v.guadagno:+.2f}€</b>\n"
            f"da {v.baseline:.2f}€ a <b>{equity:.2f}€</b>\n"
            f"Chiuse {chiuse} posizioni, tutti i grid in pausa.\n\n"
            f"Ora puoi scegliere:\n"
            f"• <b>incassare</b> il guadagno e ripartire dalla taglia di prima\n"
            f"• <b>reinvestire</b>: alzare il budget dei grid e ripartire piu' grande\n"
            f"• <b>ripartire uguale</b> senza toccare nulla\n"
            f"<i>Finche' non scegli, nessun grid apre posizioni.</i>")
        log.warning("BLOCCO profitto: %s, chiuse %d posizioni", v.messaggio, chiuse)
        return 0

    piano = pianifica_net(prezzo, p0, step, unita_correnti, max_unita=max_unita,
                          pnl_aperto=pnl, kill_pnl_eur=kill_pnl, equity=equity,
                          kill_equity_eur=kill_eq)

    log.info("%s %.4f | p0 %.4f | grad %d | netta %+.0f -> %+d | %s | %s",
             epic, prezzo, p0, piano.livello, unita_correnti, piano.unita_target,
             piano.azione, piano.motivo)

    if dry:
        print(f"\n=== {epic} (profilo {PROFILO or 'default'}) ===")
        print(f"  prezzo {prezzo:.4f} | ancoraggio {p0:.4f} | passo {step:.2%}")
        print(f"  unita': {unit_size} = {unit_size*prezzo*q2r:.2f}€ nozionale, "
              f"max {max_unita} | budget {budget:.0f}€")
        print(f"  posizione netta: {unita_correnti:+.1f} unita' -> target "
              f"{piano.unita_target:+d} | azione: {piano.azione}")
        print(f"  equity {equity:.2f}€ | P&L aperto {pnl:+.2f}€ | "
              f"mercato {snap.get('marketStatus')}")
        return 0

    if snap.get("marketStatus") != "TRADEABLE":
        log.info("mercato %s: fermo", snap.get("marketStatus"))
        return 0
    if piano.azione == "nulla":
        return 0

    size_ordine = round(abs(piano.delta) * unit_size, 8)
    if size_ordine < meta["min_size"]:
        return 0
    direzione = "BUY" if piano.delta > 0 else "SELL"
    try:
        r = capital.create_position(epic, direzione, size_ordine)
        conf = capital.confirm_deal(r.get("dealReference")) if r.get("dealReference") else {}
        if (conf.get("dealStatus") or "").upper() not in ("ACCEPTED", ""):
            log.error("ordine rifiutato: %s", conf)
            return 1
        fill = conf.get("level") or prezzo
    except Exception as exc:
        log.error("ordine fallito: %s", exc)
        return 1

    nuova = piano.unita_target
    icona = "🟢" if piano.delta > 0 else "🔴"
    if piano.azione == "kill":
        icona = "🛑"
    verso = "LONG" if nuova > 0 else ("SHORT" if nuova < 0 else "FLAT")
    telegram.send_message(
        f"{icona} <b>{epic}</b>  {'compra' if piano.delta > 0 else 'vende'} "
        f"{abs(piano.delta):.0f} unita' a {fill}\n"
        f"posizione: <b>{nuova:+d} unita' {verso}</b> "
        f"({abs(nuova)*unit_size*prezzo*q2r:.0f}€ di esposizione)\n"
        f"prezzo {prezzo:.4f} | riferimento {p0:.4f} | gradino {piano.livello}\n"
        f"P&L aperto {pnl:+.2f}€ | conto {equity:.2f}€"
        + (f"\n<i>{_esc(piano.motivo)}</i>" if piano.azione == "kill" else "")
    )
    log.info("ESEGUITO %s %s @ %s -> netta %+d unita'", direzione, size_ordine, fill, nuova)

    # stop di catastrofe sulla posizione risultante (gate: mai denaro reale senza SL)
    if nuova != 0:
        try:
            for p in capital.get_open_positions():
                po, m = p.get("position", {}), p.get("market", {})
                if m.get("epic") != epic or po.get("stopLevel"):
                    continue
                lungo = (po.get("direction") or "").upper() == "BUY"
                sl = p0 * (1 - catastrofe) if lungo else p0 * (1 + catastrofe)
                capital.update_position(po["dealId"], stop_level=round(sl, 2))
                log.info("stop di catastrofe impostato a %.2f", sl)
        except Exception:
            log.exception("impostazione stop fallita (posizione comunque protetta dal kill switch)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
