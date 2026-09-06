"""Controllo dei grid per CONTO (reale / di prova) e vocabolario dei messaggi.

Nasce dalla controanalisi del 2026-09-03: il sistema sapeva fermarsi da solo
(soglie di profitto, stop di perdita) ma l'utente poteva farlo ripartire solo
da riga di comando sulla VM. Qui stanno le operazioni che il listener Telegram
espone con /riparti e /ferma, la lettura SICURA del conto e le parole con cui
si parla all'utente (conto reale / conto di prova, nomi degli strumenti).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"

ENV_DA_PAROLA = {"reale": "live", "live": "live", "prova": "demo", "demo": "demo"}

EPIC_NOMI = {
    "US100": "Nasdaq", "US30": "Dow Jones", "US500": "S&P 500", "DE40": "DAX",
    "NL25": "AEX Olanda", "J225": "Nikkei", "HK50": "Hang Seng", "GOLD": "Oro",
    "BTCUSD": "Bitcoin", "SW20": "SMI Svizzera",
}


def nome_conto(env: str) -> str:
    return "Conto reale" if env == "live" else "Conto di prova"


def parola_conto(env: str) -> str:
    return "reale" if env == "live" else "prova"


def nome_strumento(epic: str) -> str:
    n = EPIC_NOMI.get(epic)
    return f"{n} ({epic})" if n else epic


def eur(x: float, segno: bool = False) -> str:
    """Formato italiano: 50,63 € / +0,79 €."""
    s = f"{x:+.2f}" if segno else f"{x:.2f}"
    return s.replace(".", ",") + " €"


def leggi_equity(capital) -> float | None:
    """Equity del conto, oppure None se la risposta del broker e' inutilizzabile.

    Il 03/09/2026 alle 19:07 una risposta vuota (timeout) e' stata letta come
    equity 0,00 € e lo stop di perdita ha chiuso il conto di prova per una
    "perdita" di 976,83 €. Nessuna protezione deve decidere su un dato mancante:
    chi chiama, se riceve None, NON fa nulla in questo run.
    """
    from src.capital_client import equity_conto
    try:
        accounts = capital.get_account_info().get("accounts") or []
    except Exception as exc:
        log.error("lettura conto fallita: %s", exc)
        return None
    if not accounts:
        log.error("lettura conto: nessun account nella risposta")
        return None
    bal = accounts[0].get("balance") or {}
    if bal.get("balance") is None:
        log.error("lettura conto: blocco balance assente (%s)", bal)
        return None
    eq = equity_conto(bal)
    if eq <= 0:
        log.error("lettura conto: equity non plausibile %.2f", eq)
        return None
    return eq


def avvisa_lettura_fallita(telegram, env: str, ore: float = 1.0) -> None:
    """Un solo avviso per ora e per conto: i timeout arrivano a raffica."""
    st = DATA / f"g2_lettura_{env}.json"
    now = datetime.now(timezone.utc)
    try:
        ultimo = datetime.fromisoformat(json.loads(st.read_text())["ultimo"])
        if (now - ultimo).total_seconds() < ore * 3600:
            return
    except Exception:
        pass
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"ultimo": now.isoformat()}))
    except Exception:
        pass
    try:
        telegram.send_message(
            f"⚠️ Non riesco a leggere il {nome_conto(env).lower()} (il broker non "
            f"risponde).\nNon tocco nulla e riprovo al prossimo giro. Ti avviso di "
            f"nuovo solo se dura più di un'ora.")
    except Exception:
        log.exception("avviso lettura fallita non inviato")


def stato_profitto(env: str) -> dict:
    st = DATA / f"g2_profit_{env}.json"
    try:
        return json.loads(st.read_text())
    except Exception:
        return {}


def scrivi_stato_profitto(env: str, ps: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / f"g2_profit_{env}.json").write_text(json.dumps(ps, indent=1))


def descrizione_pausa(ps: dict) -> str:
    """Perche' il conto e' fermo, in una riga, con il comando per ripartire."""
    from src.grid_report import ora_locale
    per = ps.get("bloccato_per") or "obiettivo"
    quando = ora_locale(ps.get("bloccato_il") or "", "%d/%m alle %H:%M")
    motivo = {"perdita": "per uno stop di perdita",
              "utente": "su tua richiesta",
              "obiettivo": "perché ha raggiunto l'obiettivo di guadagno"}.get(per, per)
    q = f" dal {quando}" if quando != "?" else ""
    return f"Fermo{q} {motivo}."


def _client(env: str):
    from src.capital_client import CapitalClient
    from src.config import load_config
    originale = os.environ.get("CAPITAL_ENV")
    os.environ["CAPITAL_ENV"] = env
    try:
        cfg = load_config()
        cap = CapitalClient(cfg)
        cap.login()
        return cap
    finally:
        if originale is None:
            os.environ.pop("CAPITAL_ENV", None)
        else:
            os.environ["CAPITAL_ENV"] = originale


def riparti(env: str, telegram, profit_alert: float, profit_stop: float) -> str:
    """Sblocca i grid del conto e fissa la nuova base al valore attuale."""
    cap = _client(env)
    eq = leggi_equity(cap)
    if eq is None:
        msg = (f"Non riesco a leggere il {nome_conto(env).lower()} adesso, "
               f"non ho cambiato nulla. Riprova tra qualche minuto.")
        telegram.send_message(msg)
        return msg
    scrivi_stato_profitto(env, {
        "baseline": round(eq, 2), "avvisate": [], "bloccato": False,
        "creato": datetime.now(timezone.utc).isoformat()})
    msg = (f"▶️ <b>{nome_conto(env)}: ripartito</b>\n"
           f"Nuova base: {eur(eq)}. Da qui si contano guadagni e perdite.\n"
           f"Ti avviso a {eur(profit_alert, True)}, a {eur(profit_stop, True)} "
           f"si ferma da solo per farti decidere.")
    telegram.send_message(msg)
    return msg


def ferma(env: str, telegram) -> str:
    """Chiude tutte le posizioni del conto e mette i grid in pausa."""
    cap = _client(env)
    chiuse, fallite = 0, 0
    for p in cap.get_open_positions():
        d = (p.get("position") or {}).get("dealId")
        if not d:
            continue
        try:
            cap.close_position(d)
            chiuse += 1
        except Exception as exc:
            fallite += 1
            log.error("chiusura %s fallita: %s", d, exc)
    ps = stato_profitto(env)
    eq = leggi_equity(cap)
    ps.update({"bloccato": True, "bloccato_per": "utente",
               "bloccato_il": datetime.now(timezone.utc).isoformat(),
               "bloccato_a": round(eq, 2) if eq is not None else None})
    if not ps.get("baseline"):
        ps["baseline"] = round(eq or 0, 2)
    scrivi_stato_profitto(env, ps)
    extra = f" ({fallite} non sono riuscito a chiuderle, controlla sull'app)" if fallite else ""
    msg = (f"⏹️ <b>{nome_conto(env)}: fermato</b>\n"
           f"Chiuse {chiuse} posizioni{extra}. Nessun grid apre finché non scrivi "
           f"/riparti {parola_conto(env)}.")
    telegram.send_message(msg)
    return msg


# ------------------------------------------------------- soglie per CONTO

# Le soglie di conto (perdita e profitto) sono per definizione uguali per tutti
# i grid dello stesso conto: duplicarle su ogni profilo (com'era per gli otto
# profili di prova) le fa divergere alla prima modifica dimenticata. Ordine di
# ricerca, lo stesso di jobs/grid2.py: profilo, poi CONTO, poi globale.
SOGLIE_DEFAULT = {"LOSS_ALERT_EUR": 5.0, "LOSS_STOP_EUR": 10.0,
                  "PROFIT_ALERT_EUR": 10.0, "PROFIT_STOP_EUR": 20.0}


def soglia_conto(env: str, nome: str, default: float | None = None) -> float:
    """Valore di G2_<ENV>_<nome>, altrimenti G2_<nome>, altrimenti il default."""
    if default is None:
        default = SOGLIE_DEFAULT.get(nome, 0.0)
    for chiave in (f"G2_{env.upper()}_{nome}", f"G2_{nome}"):
        v = os.environ.get(chiave)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                log.error("%s non numerico: %r", chiave, v)
    return float(default)


def soglie_conto(env: str) -> dict:
    """Le quattro soglie del conto, in euro: avviso e stop, perdita e profitto."""
    return {n.lower().replace("_eur", ""): soglia_conto(env, n)
            for n in SOGLIE_DEFAULT}
