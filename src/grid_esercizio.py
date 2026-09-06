"""ClaudeTrade: il conto da 200 € a capitale dichiarato.

Richiesta 2026-09-06: gira sul conto di prova (che ne ha circa 980), ma si
tratta come un conto normale da 200 €. Le taglie sono calibrate su quel numero
(otto strumenti a taglia minima, due gradini per verso, esposizione massima
644 € cioe' 3,2 volte il capitale) e ogni cifra del report e' riferita ai
200 €, non al saldo vero del conto.

  valore dell'esercizio = capitale + (equity - equity all'avvio)

Il conto vero resta quello che e': cambia solo la lente con cui lo si legge.
Lo stato sta in data/g2_esercizio_<env>.json e tiene anche lo storico dei
giorni, che serve per la media e per il conteggio dei giorni positivi.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.grid_control import (eur, leggi_equity, nome_strumento, soglie_conto,
                              scrivi_stato_profitto)

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"

CAPITALE_DEFAULT = 200.0


def _file(env: str) -> Path:
    return DATA / f"g2_esercizio_{env}.json"


def leggi(env: str) -> dict:
    try:
        return json.loads(_file(env).read_text())
    except Exception:
        return {}


def scrivi(env: str, st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    _file(env).write_text(json.dumps(st, indent=1, ensure_ascii=False))


def attivo(env: str) -> bool:
    return bool(leggi(env).get("capitale"))


def valore(st: dict, equity: float) -> float:
    """Il capitale dichiarato piu' quello che il conto ha guadagnato o perso."""
    return float(st["capitale"]) + (equity - float(st["equity_avvio"]))


def pct(delta: float, capitale: float) -> str:
    """Variazione in percento del capitale dichiarato, formato italiano."""
    if not capitale:
        return ""
    return f"{delta / capitale * 100:+.2f}%".replace(".", ",")


def giorni_trascorsi(st: dict) -> int:
    try:
        avvio = datetime.fromisoformat(st["avvio"])
    except Exception:
        return 0
    return max(1, (datetime.now(timezone.utc) - avvio).days + 1)


# ------------------------------------------------------------------ avvio

def azzera_stato(env: str) -> int:
    """Cancella ancoraggi, baseline di giornata e memoria dei messaggi del
    conto: l'esercizio parte da zero, senza eredita' del giro precedente."""
    tolti = 0
    for f in list(DATA.glob(f"g2_{env}_*.json")) + [DATA / f"g2_day_{env}.json",
                                                    DATA / f"g2_lettura_{env}.json"]:
        try:
            if f.exists():
                f.unlink()
                tolti += 1
        except Exception:
            log.exception("non riesco a cancellare %s", f)
    return tolti


def negoziabile(capital, epic: str) -> bool:
    """Il broker accetta ordini su questo strumento adesso?"""
    try:
        sn = capital.get_market(epic).get("snapshot") or {}
    except Exception as exc:
        log.error("stato mercato %s non leggibile: %s", epic, exc)
        return False
    return (sn.get("marketStatus") or "").upper() == "TRADEABLE"


def avvia(env: str, capitale: float, capital, telegram=None,
          chiudi: bool = True, silenzioso: bool = False) -> str:
    """Chiude quello che c'e', azzera la memoria e fissa il punto di partenza.

    Si parte solo se ogni strumento con una posizione aperta e' negoziabile:
    liquidare a mercato chiuso non si puo', e liquidare all'apertura europea
    significa vendere dentro le oscillazioni (scelta utente 2026-09-06, si
    riprova ogni dieci minuti dalla mezzanotte e si parte al primo momento
    buono). Con ``silenzioso`` un rinvio non manda nulla su Telegram.

    Ritorna il messaggio inviato (o il motivo per cui non si e' fatto nulla).
    """
    eq = leggi_equity(capital)
    if eq is None:
        msg = ("Non riesco a leggere il conto adesso: non ho toccato nulla, "
               "riprovo più tardi.")
        if telegram and not silenzioso:
            telegram.send_message(msg)
        return msg

    aperte = capital.get_open_positions()
    if chiudi and aperte:
        epics = sorted({(p.get("market") or {}).get("epic") for p in aperte} - {None})
        chiusi = [e for e in epics if not negoziabile(capital, e)]
        if chiusi:
            msg = ("Mercati ancora chiusi (" + ", ".join(chiusi)
                   + "): non parto, riprovo al prossimo giro.")
            log.info(msg)
            if telegram and not silenzioso:
                telegram.send_message(msg)
            return msg

    chiuse, fallite = 0, 0
    if chiudi:
        for p in aperte:
            d = (p.get("position") or {}).get("dealId")
            if not d:
                continue
            try:
                capital.close_position(d)
                chiuse += 1
            except Exception as exc:
                fallite += 1
                log.error("chiusura %s fallita: %s", d, exc)

    # se qualcosa e' rimasto aperto non si parte: il punto zero deve essere
    # un conto pulito, non un conto con l'eredita' del giro precedente
    if chiudi and capital.get_open_positions():
        msg = ("Non sono riuscito a chiudere tutto: non fisso la partenza, "
               "riprovo al prossimo giro.")
        log.warning(msg)
        if telegram and not silenzioso:
            telegram.send_message(msg)
        return msg

    # dopo le chiusure il saldo cambia: la base e' quello che resta adesso
    eq_dopo = leggi_equity(capital)
    if eq_dopo is not None:
        eq = eq_dopo

    azzera_stato(env)
    ora = datetime.now(timezone.utc).isoformat()
    scrivi(env, {"capitale": round(float(capitale), 2),
                 "equity_avvio": round(eq, 2), "avvio": ora, "giorni": []})
    # i grid ripartono liberi, con la baseline al valore di adesso
    scrivi_stato_profitto(env, {"baseline": round(eq, 2), "avvisate": [],
                                "bloccato": False, "creato": ora})

    s = soglie_conto(env)
    coda = f" ({fallite} non si sono chiuse, controlla sull'app)" if fallite else ""
    msg = (f"🧪 <b>ClaudeTrade: si parte</b>\n"
           f"Ho chiuso {chiuse} posizioni{coda} e azzerato la memoria: "
           f"si riparte da zero.\n\n"
           f"Capitale <b>{eur(capitale)}</b>, taglie le più piccole che il broker "
           f"accetta, su 8 strumenti, con al massimo 2 gradini per verso.\n"
           f"Obiettivo {eur(s['profit_stop'], True)}, stop {eur(-s['loss_stop'], True)}.\n"
           f"Ogni sera alle 22:30 ti mando il resoconto della giornata.")
    if telegram:
        telegram.send_message(msg)
    return msg


# ----------------------------------------------------------------- report

def registra_giorno(env: str, st: dict, riga: dict) -> dict:
    """Aggiunge (o aggiorna) la riga di oggi nello storico."""
    giorni = [g for g in st.get("giorni", []) if g.get("data") != riga["data"]]
    giorni.append(riga)
    st["giorni"] = sorted(giorni, key=lambda g: g["data"])[-400:]
    scrivi(env, st)
    return st


def _per_strumento(transazioni: list) -> list[tuple[str, float, int]]:
    """Somma delle operazioni chiuse oggi, strumento per strumento."""
    agg: dict[str, list] = {}
    for t in transazioni:
        try:
            imp = float(t.get("size") or 0)
        except (TypeError, ValueError):
            imp = 0.0
        r = agg.setdefault(t.get("instrumentName") or "?", [0.0, 0])
        r[0] += imp
        r[1] += 1
    return sorted(((k, v[0], v[1]) for k, v in agg.items()),
                  key=lambda x: -abs(x[1]))


def _num(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def _riga_posizione(p: dict) -> str:
    verso = "comprato" if p["size"] > 0 else "venduto"
    val = f", vale {eur(p['valore'])}" if p.get("valore") else ""
    return (f"• {nome_strumento(p['epic'])}: {verso} {_num(abs(p['size']))}{val}, "
            f"per ora {eur(p['pnl'], True)}")


def messaggio(c, st: dict, titolo: str = "") -> str:
    """Il resoconto della giornata, tutto rapportato al capitale dichiarato.

    ``c`` e' un src.grid_report.Conto gia' raccolto con con_valore=True.
    """
    from src.grid_report import data_estesa

    cap = float(st["capitale"])
    if not c.ok:
        return (f"🧪 <b>ClaudeTrade</b> · {data_estesa()}\n"
                "Il broker non risponde: niente resoconto per oggi.")

    val = valore(st, c.equity)
    tot = val - cap
    oggi = c.guadagno_oggi
    s = soglie_conto(c.env)
    seg = "🟢" if oggi >= 0 else "🔴"

    righe = [f"🧪 <b>{titolo or 'ClaudeTrade'}</b> · {data_estesa()}",
             "",
             f"<b>Valore: {eur(val)}</b>  ({eur(tot, True)}, {pct(tot, cap)})",
             f"Oggi: <b>{eur(oggi, True)}</b> ({pct(oggi, cap)})  {seg}"]
    if c.bloccato:
        righe.append(f"⏸ <b>Fermo.</b> {c.pausa} Per ripartire: /riparti prova")

    # --- la giornata
    righe += ["", "<b>Cosa è successo oggi</b>"]
    if c.movimenti_oggi:
        righe.append(f"• {c.movimenti_oggi} operazioni chiuse, "
                     f"{eur(c.realizzato_oggi, True)} in tutto")
        for nome, imp, n in _per_strumento(c.oggi):
            righe.append(f"   {nome_strumento(nome)}: {eur(imp, True)} ({n})")
    else:
        righe.append("• nessuna operazione chiusa")
    if c.costi_oggi:
        righe.append(f"• costi del broker: {eur(c.costi_oggi, True)}")
    non_incassato = oggi - c.realizzato_oggi - c.costi_oggi
    if abs(non_incassato) >= 0.01:
        righe.append(f"• posizioni ancora aperte: {eur(non_incassato, True)} "
                     f"di valore che si muove")

    # --- come sei messo adesso
    righe += ["", "<b>Come sei posizionato adesso</b>"]
    if c.posizioni:
        righe += [_riga_posizione(p) for p in c.posizioni]
        esposto = sum(p.get("valore") or 0 for p in c.posizioni)
        if esposto:
            righe.append(f"Esposto in tutto: <b>{eur(esposto)}</b> su {eur(cap)} "
                         f"di capitale ({_num(round(esposto / cap, 1))} volte)")
    else:
        righe.append("• nessuna posizione aperta, sei tutto liquido")

    # --- da quando e' partito
    gg = giorni_trascorsi(st)
    storico = st.get("giorni", [])
    pos = sum(1 for g in storico if g.get("delta", 0) > 0)
    neg = sum(1 for g in storico if g.get("delta", 0) < 0)
    righe += ["", f"<b>Da quando è partito ({gg} giorn{'o' if gg == 1 else 'i'})</b>",
              f"• totale {eur(tot, True)} ({pct(tot, cap)}), "
              f"media {eur(tot / gg, True)} al giorno"]
    if pos or neg:
        righe.append(f"• {pos} giornate in guadagno, {neg} in perdita")
    righe.append(f"• obiettivo {eur(s['profit_stop'], True)}: mancano "
                 f"{eur(max(0.0, s['profit_stop'] - tot))}")
    righe.append(f"• stop {eur(-s['loss_stop'], True)}: hai "
                 f"{eur(max(0.0, s['loss_stop'] + tot))} di margine")
    return "\n".join(righe)
