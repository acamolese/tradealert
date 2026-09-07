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
from src.grid_net import pianifica_net, livello, ancora_mobile, barre_chiuse
from src.grid_profit import valuta
from src.grid_control import (avvisa_lettura_fallita, eur, leggi_equity,
                              nome_conto, nome_in_frase, parola_conto)

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"
PROFILO = ""
CONTO = ""


def _env(name: str, default: str) -> str:
    """Parametro del grid: prima il profilo, poi il CONTO, poi il valore globale.

    Il livello per conto (G2_DEMO_..., G2_LIVE_...) e' del 2026-09-06: le soglie
    di conto sono per definizione uguali per tutti i grid dello stesso conto, e
    duplicarle su ogni profilo (otto volte, sul conto di prova) le fa divergere
    alla prima modifica dimenticata.
    """
    if PROFILO:
        v = os.environ.get(f"G2_{PROFILO}_{name}")
        if v is not None:
            return v
    if CONTO:
        v = os.environ.get(f"G2_{CONTO.upper()}_{name}")
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
    global PROFILO, CONTO
    if "--profile" in sys.argv:
        PROFILO = sys.argv[sys.argv.index("--profile") + 1].upper()
    dry = "--dry-run" in sys.argv
    stop = "--stop" in sys.argv
    riparti = "--riparti" in sys.argv

    from src.capital_client import CapitalClient
    from src.telegram_client import TelegramClient
    from src.executor import _market_meta

    v1 = load_config()
    CONTO = v1.capital_env
    enabled = _env("ENABLED", "false").strip().lower() == "true"
    epic = _env("EPIC", "GOLD")
    step = _f("STEP", 0.002)
    max_unita = int(_f("MAX_UNITA", 10))
    budget = _f("BUDGET_EUR", 0.0)
    kill_pnl = _f("KILL_PNL_EUR", 15.0)
    kill_eq = _f("KILL_EQUITY_EUR", 30.0)
    catastrofe = _f("CATASTROPHE", 0.20)
    # Ancoraggio MOBILE (fix 2026-08-21): periodo della media esponenziale in
    # barre giornaliere. Con ancoraggio fisso il grid si incollava al tetto dello
    # scoperto sugli asset in salita (misurato: 90% del tempo al tetto, 20% di
    # finestre positive). Con EMA5 e passo 3%: 82% di finestre positive e caso
    # peggiore -42% invece di -174%. 0 = torna al comportamento fisso.
    ema_periodo = int(_f("EMA_PERIODO", 5))
    # soglie di PROFITTO: avviso e blocco alla crescita del conto (2026-08-19)
    profit_alert = _f("PROFIT_ALERT_EUR", 10.0)
    profit_stop = _f("PROFIT_STOP_EUR", 20.0)
    # Stop di PERDITA sul CONTO, simmetrico a quello di profitto (fix 2026-08-21).
    # Mancava: il kill switch guardava il flottante di OGNI strumento separatamente
    # (-12€ ciascuno) e nessuno guardava il totale, cosi' il conto e' sceso del 20%
    # senza che nulla intervenisse. Questo guarda l'equity contro la baseline.
    loss_alert = _f("LOSS_ALERT_EUR", 5.0)
    loss_stop = _f("LOSS_STOP_EUR", 10.0)
    # Notifica per OGNI mossa: spenta di default. Con decine di operazioni al
    # giorno (186 messaggi il 20/08) diventa rumore e nasconde gli avvisi che
    # contano. Il riepilogo orario e i comandi /stat coprono il monitoraggio;
    # restano notificati solo gli eventi che richiedono attenzione: soglie di
    # profitto, kill switch, blocchi ed errori.
    notifica_mosse = _env("NOTIFICA_MOSSE", "false").strip().lower() == "true"
    # Isteresi da grid classico e ancoraggio sulle sole barre chiuse
    # (2026-09-03): disponibili ma SPENTI. Il 79% dei fill reali dista <0.15%
    # dal precedente e il passo del 3% non viene mai incassato per intero, ma il
    # backtest a 15 minuti (`jobs/grid_intraday_test.py`, 180 giorni, 8
    # strumenti, calibrato sui fill reali) dice che questo mean-reversion ad
    # alta frequenza rende +0.39€/30gg a finestra con 68% di finestre positive,
    # e l'isteresi NON lo batte (3%: +0.35€, 48%). Si accendono solo dopo un
    # nuovo backtest che dica il contrario.
    isteresi = _env("ISTERESI", "false").strip().lower() == "true"
    ema_chiusa = _env("EMA_CHIUSA", "false").strip().lower() == "true"

    if not enabled and not dry and not stop:
        log.info("profilo %s disabilitato", PROFILO or "(default)")
        return 0

    capital = CapitalClient(v1)
    capital.login()
    telegram = TelegramClient(v1)

    if riparti:
        # sblocca dopo una pausa e riparte dal valore attuale del conto: il
        # guadagno (o la perdita) diventa la nuova base. Stessa funzione del
        # comando Telegram /riparti.
        from src.grid_control import riparti as _riparti
        print(_riparti(v1.capital_env, telegram, profit_alert, profit_stop))
        return 0

    mk = capital.get_market(epic)
    snap = mk.get("snapshot", {}) or {}
    meta = _market_meta(mk, leverages_map=capital.get_leverages_map(),
                        use_real_leverage=True)
    prezzo = meta["mid_price"]
    if not prezzo:
        log.error("nessun prezzo per %s", epic)
        return 1

    # Lettura SICURA del conto (fix 2026-09-03): se il broker non risponde o
    # risponde vuoto, questo run non decide nulla. Prima una risposta vuota
    # veniva letta come equity 0,00 € e faceva scattare lo stop di perdita.
    equity = leggi_equity(capital)
    if equity is None:
        if not dry:
            avvisa_lettura_fallita(telegram, v1.capital_env)
        return 1

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
    if ema_periodo > 0:
        # riferimento = media mobile del prezzo: segue la tendenza invece di
        # restare inchiodato al giorno in cui il grid e' partito.
        prices = capital.get_prices(epic, resolution="DAY",
                                    max_bars=max(30, ema_periodo * 5))
        if ema_chiusa:
            closes = barre_chiuse(prices, datetime.now(timezone.utc).date().isoformat())
        else:
            closes = []
            for x in prices:
                cp = x.get("closePrice") or {}
                if cp.get("bid") and cp.get("ask"):
                    closes.append((float(cp["bid"]) + float(cp["ask"])) / 2)
        p0 = ancora_mobile(closes, ema_periodo) if closes else prezzo
        if not p0:
            log.error("ancoraggio non calcolabile per %s", epic)
            return 1
    elif STATE.exists():
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
        telegram.send_message(
            f"⏹️ <b>{nome_conto(v1.capital_env)}: grid {epic} fermato</b> "
            f"({len(deals)} posizioni chiuse)")
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

    # perdita: stessa meccanica, segno opposto
    perdita = float(ps["baseline"]) - equity
    # Sanity (fix 2026-09-03): una "perdita" di oltre meta' del conto in un
    # singolo run non e' un evento di mercato con queste taglie (il kill per
    # profilo scatta molto prima), e' una lettura sbagliata: non si decide.
    if perdita > 0.5 * float(ps["baseline"]):
        log.error("perdita %.2f€ non plausibile su base %.2f€: lettura sospetta, "
                  "nessuna decisione", perdita, float(ps["baseline"]))
        if not dry:
            avvisa_lettura_fallita(telegram, v1.capital_env)
        return 1
    if not ps.get("bloccato") and loss_stop > 0 and perdita >= loss_stop:
        chiuse = 0
        for d in deals:
            try:
                capital.close_position(d)
                chiuse += 1
            except Exception as exc:
                log.error("chiusura %s fallita: %s", d, exc)
        ps["bloccato"] = True
        ps["bloccato_per"] = "perdita"
        ps["bloccato_a"] = round(equity, 2)
        ps["bloccato_il"] = datetime.now(timezone.utc).isoformat()
        if not dry:
            PSTATE.write_text(json.dumps(ps, indent=1))
            telegram.send_message(
                f"🛑 <b>Stop di perdita su {nome_in_frase(v1.capital_env)}</b>\n"
                f"Il conto è sceso da {eur(float(ps['baseline']))} a "
                f"<b>{eur(equity)}</b> ({eur(-perdita, True)}).\n"
                f"Ho chiuso {chiuse} posizioni e fermato tutti i grid di questo conto.\n"
                f"Per ripartire da {eur(equity)}: /riparti {parola_conto(v1.capital_env)}")
        log.warning("STOP PERDITA: -%.2f€, chiuse %d posizioni", perdita, chiuse)
        return 0
    if (not ps.get("bloccato") and loss_alert > 0 and perdita >= loss_alert
            and -loss_alert not in [float(x) for x in ps.get("avvisate", [])]):
        ps.setdefault("avvisate", []).append(-loss_alert)
        if not dry:
            PSTATE.write_text(json.dumps(ps, indent=1))
            telegram.send_message(
                f"⚠️ <b>{nome_conto(v1.capital_env)} in perdita di {eur(perdita)}</b>\n"
                f"da {eur(float(ps['baseline']))} a {eur(equity)}.\n"
                f"Il sistema continua. Se arriva a {eur(-loss_stop, True)} chiude "
                f"tutto e si ferma da solo.")

    if v.stato == "gia_bloccato":
        log.info("%s: sistema in pausa (obiettivo raggiunto), nessuna operazione", epic)
        return 0

    if v.stato == "avviso" and not dry:
        ps.setdefault("avvisate", []).append(v.soglia_colpita)
        PSTATE.write_text(json.dumps(ps, indent=1))
        telegram.send_message(
            f"🔔 <b>{nome_conto(v1.capital_env)} cresciuto di {eur(v.guadagno, True)}</b>\n"
            f"da {eur(v.baseline)} a {eur(equity)}.\n"
            f"Il sistema continua a lavorare. A {eur(profit_stop, True)} si ferma "
            f"da solo per farti decidere cosa fare del guadagno.")

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
            f"🎯 <b>{nome_conto(v1.capital_env)}: obiettivo raggiunto, "
            f"{eur(v.guadagno, True)}</b>\n"
            f"da {eur(v.baseline)} a <b>{eur(equity)}</b>.\n"
            f"Ho chiuso {chiuse} posizioni e fermato i grid di questo conto.\n\n"
            f"Ora puoi scegliere:\n"
            f"• <b>incassare</b>: prelevi il guadagno e riparti come prima\n"
            f"• <b>reinvestire</b>: alzi i budget e riparti più grande\n"
            f"• <b>ripartire uguale</b>: /riparti {parola_conto(v1.capital_env)}\n"
            f"<i>Finché non scegli, nessun grid apre posizioni.</i>")
        log.warning("BLOCCO profitto: %s, chiuse %d posizioni", v.messaggio, chiuse)
        return 0

    piano = pianifica_net(prezzo, p0, step, unita_correnti, max_unita=max_unita,
                          pnl_aperto=pnl, kill_pnl_eur=kill_pnl, equity=equity,
                          kill_equity_eur=kill_eq, isteresi=isteresi)

    log.info("%s %.4f | p0 %.4f | grad %d | netta %+.0f -> %+d | %s | %s",
             epic, prezzo, p0, piano.livello, unita_correnti, piano.unita_target,
             piano.azione, piano.motivo)

    if dry:
        print(f"\n=== {epic} (profilo {PROFILO or 'default'}) ===")
        print(f"  prezzo {prezzo:.4f} | ancoraggio {p0:.4f} | passo {step:.2%} | "
              f"isteresi {'si' if isteresi else 'no'} | EMA {'chiusa' if ema_chiusa else 'con barra corrente'}")
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
    icona = "🛑" if piano.azione == "kill" else ("🟢" if piano.delta > 0 else "🔴")
    verso = "LONG" if nuova > 0 else ("SHORT" if nuova < 0 else "FLAT")
    if piano.azione == "kill":
        from src.grid_control import nome_strumento
        telegram.send_message(
            f"🛑 <b>{nome_conto(v1.capital_env)}: chiuso {nome_strumento(epic)}</b>\n"
            f"Perdita aperta oltre il limite di questo strumento: ho azzerato la "
            f"posizione ({eur(pnl, True)}). Gli altri grid continuano.\n"
            f"<i>{_esc(piano.motivo)}</i>")
    elif notifica_mosse:
        telegram.send_message(
            f"{icona} <b>{epic}</b>  {'compra' if piano.delta > 0 else 'vende'} "
            f"{abs(piano.delta):.0f} unita' a {fill}\n"
            f"posizione: <b>{nuova:+d} unita' {verso}</b> "
            f"({abs(nuova)*unit_size*prezzo*q2r:.0f}€ di esposizione)\n"
            f"prezzo {prezzo:.4f} | riferimento {p0:.4f} | gradino {piano.livello}\n"
            f"P&L aperto {pnl:+.2f}€ | conto {equity:.2f}€")
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
