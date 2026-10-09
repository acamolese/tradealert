"""Orchestrazione della vendita di assicurazione sulla volatilita'.

Mette insieme i pezzi: i parametri (`vol_config`), le decisioni (`volatilita`),
il segnale di mercato (`vol_segnale`), il broker e la registrazione su Supabase
(`vol_store`). Il job in `jobs/paura_esegui.py` e' solo l'ingresso.

Cosa cambia rispetto alla versione con esposizione fissa, in una riga: la taglia
non e' piu' sempre il 15% del capitale, ma un gradino fra 0% e il tetto, scelto
su condizioni verificabili e registrato per intero. Il tetto e lo stop in euro
non sono cambiati, quindi il rischio massimo autorizzato e' lo stesso di prima.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import vol_segnale
from src.vol_config import carica
from src.vol_store import VolStore
from src.volatilita import (
    Conto, Gradino, Parametri, Segnale, conto_autorizzato, fine_pausa, in_pausa,
    livello_stop, ordine_accettato, perdita_da_salto, scala, segno_verso,
    serve_ribilancio, stop_colpito, unita_target, verso_consentito,
)

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"
STATO = DATA / "paura.json"


# --- stato locale ---------------------------------------------------------
# Il file resta la fonte di verita' operativa anche quando Supabase e' muto:
# il sistema deve saper decidere da solo se e' in pausa, sempre.

def stato() -> dict:
    try:
        return json.loads(STATO.read_text())
    except Exception:
        return {}


def scrivi(d: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATO.write_text(json.dumps(d, indent=1))


def _giorni_da(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        quando = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - quando).days)


def conto_da_stato(st: dict, pnl_aperto: float) -> Conto:
    """Cosa sappiamo della storia del sistema, letto dal file di stato."""
    return Conto(
        giorni_operativi=_giorni_da(st.get("avviata")) or 0,
        risultato_cumulato_eur=float(st.get("realizzato") or 0.0) + pnl_aperto,
        giorni_da_ultimo_stop=_giorni_da(st.get("ultimo_stop_il")),
        in_pausa=in_pausa(st),
    )


# --- lettura del broker ---------------------------------------------------

def _posizione(cap: Any, epic: str) -> tuple[float, float, list[str]]:
    """Size netta (negativa se corta), risultato aperto, dealId da chiudere."""
    size, pnl, deals = 0.0, 0.0, []
    for p in cap.get_open_positions():
        po, m = p.get("position", {}), p.get("market", {})
        if m.get("epic") != epic:
            continue
        s = float(po.get("size") or 0)
        size += -s if (po.get("direction") or "").upper() == "SELL" else s
        pnl += float(po.get("upl") or 0)
        if po.get("dealId"):
            deals.append(po["dealId"])
    return size, pnl, deals


def _mercato(cap: Any, epic: str) -> dict[str, Any] | None:
    from src.executor import _market_meta
    from src.risk import quote_to_ref_factor

    mk = cap.get_market(epic)
    sn = mk.get("snapshot") or {}
    inst = mk.get("instrument") or {}
    bid, ask = float(sn.get("bid") or 0), float(sn.get("offer") or 0)
    if not bid or not ask:
        log.info("nessun prezzo per %s", epic)
        return None
    meta = _market_meta(mk)
    q2r = quote_to_ref_factor(inst.get("currency"), cap) or 1.0
    prezzo = (bid + ask) / 2
    return {
        "prezzo": prezzo,
        "aperto": (sn.get("marketStatus") or "").upper() == "TRADEABLE",
        "modi": list(sn.get("marketModes") or []),
        "meta": meta,
        "noz_unita": meta["min_size"] * prezzo * q2r,
        "q2r": q2r,
    }


# --- messaggi -------------------------------------------------------------

def quote_extra(epic: str) -> int:
    """Quote in piu' decise dalla scala per questo strumento. 0 se non si sa."""
    try:
        from src.scalata import extra_per
        return max(0, int(extra_per(epic, "demo")))
    except Exception:
        return 0


def _spiega_gradino(g: Gradino, capitale: float, par: Parametri) -> str:
    noz = capitale * g.frazione
    if g.frazione <= 0:
        return "Esposizione a zero: resto fuori."
    salto = par.salto_catastrofe
    return (f"Esposizione al {g.frazione:.0%} del capitale ({noz:.0f} € su "
            f"{capitale:.0f} €). Se lo strumento andasse contro del {salto:.0%} "
            f"in una notte, come nel peggior giorno della sua storia, perderei "
            f"circa {perdita_da_salto(noz, salto):.0f} €.")


# --- il giro completo -----------------------------------------------------

def esegui(argv: list[str] | None = None) -> int:
    """Un run del sistema. Ritorna il codice di uscita del processo."""
    argv = sys.argv if argv is None else argv
    os.environ["CAPITAL_ENV"] = "demo"          # guard: mai sul conto reale
    from src.config import load_config

    cfg = load_config()
    if not conto_autorizzato(cfg.capital_env):
        log.error("conto '%s' non autorizzato: questa strategia gira solo sul "
                  "conto di prova. Interrompo senza toccare nulla.", cfg.capital_env)
        return 1

    par = carica()
    from src.capital_client import CapitalClient
    from src.grid_esercizio import leggi as leggi_esercizio
    from src.telegram_client import TelegramClient
    from src.grid_control import eur

    cap = CapitalClient(cfg)
    cap.login()
    tg = TelegramClient(cfg)
    store = VolStore(_database(cfg))
    st = stato()

    es = leggi_esercizio("demo")
    capitale = float(es.get("capitale") or par.capitale_default)

    mk = _mercato(cap, par.epic)
    if not mk:
        return 0
    size, pnl, deals = _posizione(cap, par.epic)
    noz_attuale = abs(size) * mk["prezzo"] * mk["q2r"]
    conto = conto_da_stato(st, pnl)
    segnale = vol_segnale.leggi(cap) if "--stato" in argv or mk["aperto"] else Segnale()
    gradino = scala(segnale, conto, par)

    if "--stato" in argv:
        _stampa_stato(par, mk, size, pnl, noz_attuale, capitale, segnale, gradino, st)
        return 0

    if "--chiudi" in argv:
        for d in deals:
            cap.close_position(d)
        _registra_chiusura(store, st, par, mk, pnl, len(deals), "manuale")
        tg.send_message(f"⏹️ <b>Assicurazione: chiusa</b>\nChiuse {len(deals)} posizioni.")
        return 0

    # Fail-closed sul verso: se il broker non permette di aprire cosi', meglio
    # non fare niente e dirlo, che mandare ordini destinati al rifiuto.
    if not verso_consentito(mk["modi"], par.verso):
        log.error("%s: il broker non permette il verso '%s' (marketModes=%s). "
                  "Nessun ordine.", par.epic, par.verso, mk["modi"])
        store.decisione(epic=par.epic, gradino=gradino, azione="rifiutato",
                        eseguito=False, segnale=segnale, conto=conto,
                        capitale=capitale, nozionale_attuale=noz_attuale,
                        size_prima=size, risultato_eur=pnl,
                        motivo=f"verso {par.epic} non consentito: {mk['modi']}")
        if "--stato" not in argv and mk["aperto"]:
            tg.send_message(
                f"⚠️ <b>Assicurazione: non posso aprire</b>\n"
                f"Il broker non permette di stare {'corti' if segno_verso(par.verso) < 0 else 'lunghi'} "
                f"su {par.epic} (modalità {', '.join(mk['modi']) or 'sconosciuta'}). "
                f"Non ho mandato nessun ordine: va cambiato strumento o verso.")
        return 1

    if not mk["aperto"]:
        log.info("%s: mercato chiuso, non tocco nulla", par.epic)
        store.decisione(epic=par.epic, gradino=gradino, azione="nessuna",
                        eseguito=False, segnale=segnale, conto=conto,
                        capitale=capitale, nozionale_attuale=noz_attuale,
                        size_prima=size, risultato_eur=pnl)
        return 0

    # Kill switch: ha la precedenza su qualunque valutazione del mercato.
    if stop_colpito(pnl, size, par):
        for d in deals:
            cap.close_position(d)
        _registra_chiusura(store, st, par, mk, pnl, len(deals), "stop")
        store.decisione(epic=par.epic, gradino=Gradino("fermo", 0.0, ["stop di perdita"]),
                        azione="stop", eseguito=True, segnale=segnale, conto=conto,
                        capitale=capitale, nozionale_attuale=noz_attuale,
                        size_prima=size, size_dopo=0.0, risultato_eur=pnl)
        tg.send_message(
            f"🛑 <b>Assicurazione: stop</b>\n"
            f"La posizione su {par.epic} ha perso {eur(-pnl)}, oltre il limite di "
            f"{eur(par.stop_eur)}. Ho chiuso tutto.\nNon riapro per "
            f"{par.pausa_giorni} giorni: dopo uno strappo la volatilità resta alta "
            f"e riaprire subito è la cosa peggiore.")
        log.warning("STOP: perdita %.2f€, pausa di %d giorni", pnl, par.pausa_giorni)
        return 0

    if conto.in_pausa:
        log.info("in pausa fino al %s", (st.get("in_pausa_fino") or "")[:10])
        store.decisione(epic=par.epic, gradino=gradino, azione="pausa", eseguito=False,
                        segnale=segnale, conto=conto, capitale=capitale,
                        nozionale_attuale=noz_attuale, size_prima=size,
                        risultato_eur=pnl)
        return 0

    # Il gradino dice la frazione, le quote sono intere: si ragiona in quote.
    # La scala (src/scalata.py, 2026-10-09) puo' aggiungere quote comprate con i
    # soldi incassati dal conto: valgono solo se il gradino e' sopra zero (la
    # ritirata resta ritirata) e alzano il tetto della stessa misura.
    obiettivo = capitale * gradino.frazione
    unita_base = unita_target(obiettivo, mk["noz_unita"])
    extra = quote_extra(par.epic) if unita_base > 0 else 0
    unita_tetto = max(1, round(capitale * par.tetto / mk["noz_unita"])) + extra
    unita = min(unita_base + extra, unita_tetto)
    obiettivo = unita * mk["noz_unita"]
    massimo = unita_tetto * mk["noz_unita"]
    if noz_attuale > massimo * 1.01:
        log.warning("esposizione %.2f€ oltre il tetto %.2f€ (%d quote): riduco",
                    noz_attuale, massimo, unita_tetto)
    elif not serve_ribilancio(noz_attuale, obiettivo, size, par):
        log.info("%s: esposizione %.2f€ in linea con l'obiettivo %.2f€ (gradino %s, "
                 "%d quote di cui %d dalla scala), non tocco", par.epic, noz_attuale,
                 obiettivo, gradino.nome, unita, extra)
        store.decisione(epic=par.epic, gradino=gradino, azione="nessuna", eseguito=False,
                        segnale=segnale, conto=conto, capitale=capitale,
                        nozionale_attuale=noz_attuale, nozionale_target=obiettivo,
                        size_prima=size, size_dopo=size, risultato_eur=pnl)
        _ricorda_gradino(st, gradino)
        return 0

    size_target = segno_verso(par.verso) * unita * mk["meta"]["min_size"]
    delta = size_target - size
    if abs(delta) < mk["meta"]["min_size"] / 2:
        return 0

    # Uscita completa: si chiude, non si apre il verso opposto.
    if unita == 0:
        for d in deals:
            cap.close_position(d)
        _registra_chiusura(store, st, par, mk, pnl, len(deals), "ritirata")
        store.decisione(epic=par.epic, gradino=gradino, azione="chiusura", eseguito=True,
                        segnale=segnale, conto=conto, capitale=capitale,
                        nozionale_attuale=noz_attuale, nozionale_target=0.0,
                        size_prima=size, size_dopo=0.0, risultato_eur=pnl)
        tg.send_message(
            f"🌧️ <b>Assicurazione: mi tiro fuori</b>\n"
            f"{'; '.join(gradino.motivi)}.\n\n"
            f"Ho chiuso la posizione su {par.epic} ({eur(pnl, True)}). "
            f"Rientro quando la curva torna normale: il guadagno di questa "
            f"strategia esiste solo finché la protezione costa più di quanto vale.")
        _ricorda_gradino(st, gradino)
        return 0

    noz_target = unita * mk["noz_unita"]
    verso = "SELL" if delta < 0 else "BUY"
    # lo stop del broker protegge la posizione risultante, qualunque sia il verso
    stop_level = livello_stop(mk["prezzo"], noz_target, par) if size_target else None
    azione = "apertura" if not size else ("aumento" if noz_target > noz_attuale
                                          else "riduzione")
    log.info("%s: gradino %s, da %+.0f a %+.0f unita (%s %.4f), esposizione "
             "%.2f -> %.2f€, stop a %s", par.epic, gradino.nome, size, size_target,
             verso, abs(delta), noz_attuale, noz_target, stop_level)

    # L'ordine e' asincrono: il 200 con dealReference dice solo "ricevuto". Il
    # verdetto sta in /confirms, e senza un ACCEPTED esplicito l'ordine NON e'
    # stato eseguito (14 rifiuti silenziosi fra il 16/09 e il 05/10/2026).
    conferma: dict[str, Any] = {}
    errore = ""
    try:
        esito = cap.create_position(epic=par.epic, direction=verso, size=abs(delta),
                                    stop_level=stop_level)
        ref = (esito or {}).get("dealReference")
        conferma = cap.confirm_deal(ref) if ref else {}
    except Exception as exc:   # rete, 4xx, conferma mai indicizzata
        errore = str(exc)[:300]
    if not ordine_accettato(conferma):
        motivo = (conferma.get("rejectReason") or conferma.get("dealStatus")
                  or errore or "nessuna conferma")
        log.error("%s: ordine %s %.4f NON eseguito: %s", par.epic, verso,
                  abs(delta), motivo)
        store.decisione(epic=par.epic, gradino=gradino, azione=azione, eseguito=False,
                        segnale=segnale, conto=conto, capitale=capitale,
                        nozionale_attuale=noz_attuale, nozionale_target=noz_target,
                        size_prima=size, size_dopo=size, risultato_eur=pnl,
                        stop_level=stop_level, motivo=f"rifiutato: {motivo}")
        tg.send_message(
            f"⚠️ <b>Assicurazione: ordine rifiutato</b>\n"
            f"Volevo portare {par.epic} da {size:+.0f} a {size_target:+.0f} quote, il "
            f"broker ha risposto: <i>{motivo}</i>.\n"
            f"La posizione resta quella che è ({eur(noz_attuale)} di esposizione). "
            f"Riprovo al prossimo giro.")
        return 1

    fill = conferma.get("level") or mk["prezzo"]
    log.info("%s: ESEGUITO %s %.4f a %s (deal %s)", par.epic, verso, abs(delta),
             fill, conferma.get("dealId"))
    store.decisione(epic=par.epic, gradino=gradino, azione=azione, eseguito=True,
                    segnale=segnale, conto=conto, capitale=capitale,
                    nozionale_attuale=noz_attuale, nozionale_target=noz_target,
                    size_prima=size, size_dopo=size_target, risultato_eur=pnl,
                    stop_level=stop_level)
    store.apertura(epic=par.epic, deal_id=conferma.get("dealId") or (esito or {}).get("dealReference"),
                   verso="short" if size_target < 0 else "long",
                   size=abs(size_target), prezzo=float(fill))
    scrivi({**st, "ultimo_ordine": datetime.now(timezone.utc).isoformat(),
            "unita": unita, "in_pausa_fino": None, "gradino": gradino.nome})

    _avvisa(tg, st, par, gradino, capitale, unita, mk, eur)
    return 0


def _avvisa(tg: Any, st: dict, par: Parametri, gradino: Gradino, capitale: float,
            unita: int, mk: dict, eur: Any) -> None:
    """Telegram solo quando cambia qualcosa di sostanziale.

    I ribilanci dentro lo stesso gradino non si annunciano: il 20 agosto un
    sistema che notificava ogni mossa ha prodotto 186 messaggi in un giorno.
    """
    noz = unita * mk["noz_unita"]
    if not st.get("avviata"):
        if segno_verso(par.verso) < 0:
            come = (f"Vendo {unita} quote di {par.epic} allo scoperto: {eur(noz)} di "
                    f"esposizione su {eur(capitale)}.\n\n"
                    f"Non prevede niente: quello strumento perde valore per come è "
                    f"costruito, e io incasso quel calo.")
        else:
            come = (f"Compro {unita} quote di {par.epic}: {eur(noz)} di esposizione "
                    f"su {eur(capitale)}.\n\n"
                    f"Non prevede niente: quello strumento è costruito per incassare "
                    f"il premio che pagano i compratori di protezione, e sale finché "
                    f"i mercati restano calmi.")
        tg.send_message(
            f"🛡️ <b>Assicurazione: partita</b>\n{come} Si guadagna poco quasi "
            f"sempre e si perde molto di rado: se perde {eur(par.stop_eur)} chiudo "
            f"e resto fermo {par.pausa_giorni} giorni.")
        scrivi({**stato(), "avviata": datetime.now(timezone.utc).isoformat(),
                "gradino": gradino.nome})
        return

    if st.get("gradino") and st["gradino"] != gradino.nome:
        su = gradino.frazione > _frazione_di(st["gradino"], par)
        tg.send_message(
            f"{'📈' if su else '📉'} <b>Assicurazione: {'alzo' if su else 'abbasso'} "
            f"la taglia</b>\n"
            f"{'; '.join(gradino.motivi)}.\n\n"
            f"{_spiega_gradino(gradino, capitale, par)}")


def _frazione_di(nome: str, par: Parametri) -> float:
    return {"fermo": 0.0, "ritirata": 0.0, "base": par.frazione_base,
            "favorevole": par.frazione_favorevole,
            "pieno": par.frazione_piena}.get(nome, par.frazione_base)


def _ricorda_gradino(st: dict, gradino: Gradino) -> None:
    if st.get("gradino") != gradino.nome:
        scrivi({**st, "gradino": gradino.nome})


def _registra_chiusura(store: VolStore, st: dict, par: Parametri, mk: dict,
                       pnl: float, quante: int, motivo: str) -> None:
    """Chiude nel DB e accumula il realizzato nello stato locale.

    Il realizzato locale serve alla scala: senza di esso, dopo una chiusura il
    sistema non saprebbe piu' se ha diritto di salire di gradino.
    """
    store.chiusura(epic=par.epic, prezzo=mk["prezzo"], risultato_eur=pnl, motivo=motivo)
    nuovo = {**st, "realizzato": float(st.get("realizzato") or 0.0) + pnl,
             "gradino": "fermo" if motivo == "stop" else "ritirata"}
    if motivo == "stop":
        nuovo["in_pausa_fino"] = fine_pausa(datetime.now(timezone.utc), par)
        nuovo["ultimo_stop_il"] = datetime.now(timezone.utc).isoformat()
        nuovo["ultimo_stop"] = pnl
    if motivo == "manuale":
        nuovo["chiuso_il"] = datetime.now(timezone.utc).isoformat()
    scrivi(nuovo)
    log.info("chiusura registrata: %d posizioni, motivo %s, risultato %.2f€",
             quante, motivo, pnl)


def _stampa_stato(par: Parametri, mk: dict, size: float, pnl: float,
                  noz_attuale: float, capitale: float, segnale: Segnale,
                  gradino: Gradino, st: dict) -> None:
    from src.grid_control import eur

    print(f"{par.epic}: prezzo {mk['prezzo']:.2f}, mercato "
          f"{'aperto' if mk['aperto'] else 'chiuso'}")
    print(f"posizione: {size:+.0f} unita = {eur(noz_attuale)} di esposizione, "
          f"risultato {eur(pnl, True)}")
    print(f"mercato:   {vol_segnale.racconta(segnale)}")
    print(f"gradino:   {gradino.nome} -> {eur(capitale * gradino.frazione)} "
          f"({gradino.frazione:.0%} di {eur(capitale)})")
    ex = quote_extra(par.epic)
    if ex:
        print(f"scala:     +{ex} quote dalla scala ({eur(ex * mk['noz_unita'])})")
    for m in gradino.motivi:
        print(f"           · {m}")
    if st.get("in_pausa_fino"):
        print(f"in pausa fino al {st['in_pausa_fino'][:10]}")


def _database(cfg: Any) -> Any | None:
    try:
        from src.db import Database
        return Database(cfg)
    except Exception as exc:
        log.error("Supabase non disponibile (%s): il sistema opera comunque, "
                  "ma questo run non sara' analizzabile", exc)
        return None


# --- riconciliazione ------------------------------------------------------

def riconcilia(cfg: Any) -> dict[str, int]:
    """Allinea le posizioni registrate con quelle vive sul broker.

    Chiamata dal job orario `jobs/reconcile.py`. Lavora sempre sul conto di
    prova, perche' e' l'unico dove questa strategia esiste.
    """
    from src.capital_client import CapitalClient

    par = carica()
    cfg_demo = replace(cfg, capital_env="demo")
    esito = {"registrate": 0, "vive": 0, "chiuse": 0}
    store = VolStore(_database(cfg_demo))
    if not store.attivo:
        return esito

    aperte = store.posizioni_aperte(par.epic)
    esito["registrate"] = len(aperte)
    if not aperte:
        return esito

    try:
        cap = CapitalClient(cfg_demo)
        cap.login()
        size, pnl, _ = _posizione(cap, par.epic)
        prezzo = (_mercato(cap, par.epic) or {}).get("prezzo")
    except Exception as exc:
        log.error("riconciliazione volatilita' saltata (broker): %s", exc)
        return esito

    if size:
        esito["vive"] = len(aperte)
        return esito

    # Nessuna posizione sul broker ma righe aperte nel DB: e' sparita altrove
    # (stop del broker, chiusura manuale dal frontend).
    esito["chiuse"] = store.chiusura(epic=par.epic, prezzo=prezzo,
                                     risultato_eur=None,
                                     motivo="sparita_dal_broker")
    log.info("riconciliazione volatilita': chiuse %d posizioni orfane", esito["chiuse"])
    return esito
