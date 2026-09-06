"""Riepilogo dell'attivita' dei grid sui DUE conti, in parole semplici.

Serve i messaggi automatici (mattina, sera) e i comandi Telegram su richiesta
(/stato, /posizioni, /oggi). Fonte dei numeri: sempre il BROKER
(/history/transactions + posizioni + saldo), mai il DB, i cui pnl hanno segno
inaffidabile.

Riscritto il 2026-09-03 (controanalisi): il vecchio riepilogo orario parlava la
lingua del broker (equity, flottante, realizzato, mosse, baseline), metteva tre
P&L diversi nello stesso blocco e arrivava 24 volte al giorno anche a mercati
chiusi. Regole nuove: una domanda per riga (quanto ho, come e' andata oggi,
come va da quando e' partito), parole di tutti i giorni, il dettaglio si chiede.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.grid_control import (descrizione_pausa, eur, leggi_equity, nome_conto,
                              nome_strumento, parola_conto)

ROMA = ZoneInfo("Europe/Rome")
DATA = Path(__file__).resolve().parent.parent / "data"

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def ora_locale(iso_utc: str, fmt: str = "%d/%m %H:%M") -> str:
    """Converte un timestamp UTC del broker in ora italiana (solo per la
    stampa: i confronti restano in UTC)."""
    if not iso_utc:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ROMA).strftime(fmt)
    except ValueError:
        return iso_utc[:16]


def data_estesa(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(ROMA)
    return f"{GIORNI[dt.weekday()]} {dt.day} {MESI[dt.month - 1]}"


def _amount(t: dict) -> float:
    try:
        return float(t.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Conto:
    nome: str
    env: str
    ok: bool = True                 # False = broker non leggibile
    equity: float = 0.0
    saldo: float = 0.0
    flottante: float = 0.0
    baseline: float | None = None
    baseline_data: str = ""
    equity_giorno: float | None = None
    bloccato: bool = False
    pausa: str = ""
    posizioni: list = field(default_factory=list)
    movimenti_oggi: int = 0
    realizzato_oggi: float = 0.0
    costi_oggi: float = 0.0
    oggi: list = field(default_factory=list)
    ultimi: list = field(default_factory=list)

    @property
    def guadagno(self) -> float:
        return (self.equity - self.baseline) if self.baseline else 0.0

    @property
    def guadagno_oggi(self) -> float:
        """Delta equity da inizio giornata (mezzanotte italiana): include le
        posizioni aperte, che il solo realizzato nasconde."""
        return (self.equity - self.equity_giorno) if self.equity_giorno else 0.0


def _baseline_giorno(env: str, equity: float) -> float:
    """Equity alla PRIMA lettura del giorno (ora italiana), persistita su file."""
    st = DATA / f"g2_day_{env}.json"
    oggi = datetime.now(ROMA).date().isoformat()
    try:
        d = json.loads(st.read_text())
        if d.get("data") == oggi:
            return float(d["equity"])
    except Exception:
        pass
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"data": oggi, "equity": round(equity, 2)}))
    except Exception:
        pass
    return equity


def raccogli(capital, env: str, nome: str = "", n_ultimi: int = 10,
             con_valore: bool = False) -> Conto:
    """Fotografia di un conto: saldo, posizioni, movimenti e P&L da broker."""
    from jobs.account_truth import fetch_transactions
    from src.capital_client import cash_conto, flottante_conto

    c = Conto(nome=nome or nome_conto(env), env=env)
    eq = leggi_equity(capital)
    if eq is None:
        c.ok = False
        return c
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    c.equity = eq
    c.flottante = flottante_conto(bal)
    c.saldo = cash_conto(bal)

    st = DATA / f"g2_profit_{env}.json"
    if st.exists():
        try:
            d = json.loads(st.read_text())
            c.baseline = float(d.get("baseline") or 0) or None
            c.bloccato = bool(d.get("bloccato"))
            c.baseline_data = ora_locale(d.get("creato") or "", "%d/%m")
            if c.bloccato:
                c.pausa = descrizione_pausa(d)
        except Exception:
            pass
    c.equity_giorno = _baseline_giorno(env, c.equity)

    # aggregate per strumento: il broker tiene righe separate per ogni ordine
    # nella stessa direzione, ma per il grid conta la posizione NETTA.
    agg: dict[str, dict] = {}
    for p in capital.get_open_positions():
        po, m = p.get("position", {}), p.get("market", {})
        s = float(po.get("size") or 0)
        if (po.get("direction") or "").upper() == "SELL":
            s = -s
        ep = m.get("epic") or "?"
        r = agg.setdefault(ep, {"epic": ep, "size": 0.0, "pnl": 0.0, "righe": 0,
                                "level": float(po.get("level") or 0),
                                "bid": float(m.get("bid") or 0), "valore": None})
        r["size"] += s
        r["pnl"] += float(po.get("upl") or 0)
        r["righe"] += 1
    if con_valore:
        from src.risk import quote_to_ref_factor
        for r in agg.values():
            try:
                mk = capital.get_market(r["epic"])
                q2r = quote_to_ref_factor((mk.get("instrument") or {}).get("currency"),
                                          capital) or 1.0
                px = r["bid"] or float((mk.get("snapshot") or {}).get("bid") or 0)
                r["valore"] = abs(r["size"]) * px * q2r
            except Exception:
                pass
    c.posizioni = sorted(agg.values(), key=lambda r: r["epic"])

    tx = fetch_transactions(capital, 2)
    lim_gg = (datetime.now(ROMA).replace(hour=0, minute=0, second=0, microsecond=0)
              .astimezone(timezone.utc).isoformat())
    for t in tx:
        d = t.get("dateUtc") or ""
        tipo = t.get("transactionType")
        imp = _amount(t)
        if d >= lim_gg:
            if tipo == "TRADE":
                c.movimenti_oggi += 1
                c.realizzato_oggi += imp
                c.oggi.append(t)
            elif tipo in ("SWAP", "CORPORATE_ACTION"):
                c.costi_oggi += imp
    c.oggi.reverse()
    c.ultimi = [t for t in reversed(tx) if t.get("transactionType") == "TRADE"][:n_ultimi]
    return c


# ---------------------------------------------------------------- messaggi

def _icona(c: Conto) -> str:
    return "💶" if c.env == "live" else "🧪"


def _blocco_conto(c: Conto, con_posizioni: bool = True) -> str:
    if not c.ok:
        return (f"{_icona(c)} <b>{c.nome}</b>: il broker non risponde, "
                f"riprovo più tardi.")
    seg = "🟢" if c.guadagno_oggi >= 0 else "🔴"
    righe = [f"{_icona(c)} <b>{c.nome}: {eur(c.equity)}</b>"
             + ("  ⏸ FERMO" if c.bloccato else ""),
             f"   oggi: <b>{eur(c.guadagno_oggi, True)}</b>  {seg}"]
    if c.baseline:
        avvio = f" ({c.baseline_data})" if c.baseline_data else ""
        righe.append(f"   da quando è partito{avvio}: {eur(c.guadagno, True)}")
    if c.bloccato:
        righe.append(f"   {c.pausa}")
        righe.append(f"   Per farlo ripartire: /riparti {parola_conto(c.env)}")
    if con_posizioni:
        n = len(c.posizioni)
        if n:
            righe.append(f"   posizioni aperte: {n} (in tutto {eur(c.flottante, True)} "
                         f"non ancora incassati)")
        else:
            righe.append("   posizioni aperte: nessuna")
    return "\n".join(righe)


NESSUN_CONTO = "Non riesco a leggere i conti adesso, riprova tra qualche minuto."


def messaggio_stato(conti: list[Conto], titolo: str = "Situazione adesso") -> str:
    if not conti:
        return NESSUN_CONTO
    corpo = "\n\n".join(_blocco_conto(c) for c in conti)
    costi = sum(c.costi_oggi for c in conti if c.ok)
    ops = sum(c.movimenti_oggi for c in conti if c.ok)
    coda = (f"\n\n<i>Oggi: {ops} operazioni chiuse, costi del broker "
            f"{eur(costi, True)}. Dettagli: /posizioni, /oggi</i>")
    return f"📊 <b>{titolo}</b> · {data_estesa()}\n\n{corpo}{coda}"


def messaggio_sera(conti: list[Conto]) -> str:
    return messaggio_stato(conti, "Chiusura di giornata").replace("📊", "🌙", 1)


def messaggio_mattina(conti: list[Conto], sistema_ok: bool | None = None) -> str:
    if not conti:
        return NESSUN_CONTO
    righe = [f"☀️ <b>Buongiorno</b> · {data_estesa()}"]
    for c in conti:
        if not c.ok:
            righe.append(f"{_icona(c)} {c.nome}: il broker non risponde.")
            continue
        stato = " (fermo)" if c.bloccato else ""
        righe.append(f"{_icona(c)} {c.nome}: {eur(c.equity)}{stato}"
                     + (f", da quando è partito {eur(c.guadagno, True)}" if c.baseline else ""))
    if sistema_ok is True:
        righe.append("Sistema: ✅ tutto regolare.")
    elif sistema_ok is False:
        righe.append("Sistema: ⚠️ c'è un avviso, guarda il messaggio del controllo.")
    fermi = [c for c in conti if c.ok and c.bloccato]
    for c in fermi:
        righe.append(f"{c.nome}: {c.pausa} Per ripartire: /riparti {parola_conto(c.env)}")
    return "\n".join(righe)


def _riga_posizione(p: dict) -> str:
    verso = "comprato" if p["size"] > 0 else "venduto"
    val = f", vale {eur(p['valore'])}" if p.get("valore") else f" ({abs(p['size']):g})"
    return f"• {nome_strumento(p['epic'])}: {verso}{val}, per ora {eur(p['pnl'], True)}"


def messaggio_posizioni(conti: list[Conto]) -> str:
    if not conti:
        return NESSUN_CONTO
    blocchi = []
    for c in conti:
        if not c.ok:
            blocchi.append(f"{_icona(c)} <b>{c.nome}</b>: il broker non risponde.")
            continue
        if not c.posizioni:
            blocchi.append(f"{_icona(c)} <b>{c.nome}</b>: nessuna posizione aperta.")
            continue
        righe = [f"{_icona(c)} <b>{c.nome}</b>"] + [_riga_posizione(p) for p in c.posizioni]
        blocchi.append("\n".join(righe))
    nota = ("\n\n<i>Il grid tiene queste posizioni come scorta: è normale che siano "
            "in leggera perdita finché il prezzo non torna al livello di vendita.</i>")
    return "📌 <b>Posizioni aperte</b>\n\n" + "\n\n".join(blocchi) + nota


def messaggio_oggi(conti: list[Conto], n: int = 0) -> str:
    """Operazioni chiuse oggi (o le ultime n se n > 0)."""
    if not conti:
        return NESSUN_CONTO
    blocchi = []
    for c in conti:
        if not c.ok:
            blocchi.append(f"{_icona(c)} <b>{c.nome}</b>: il broker non risponde.")
            continue
        lista = c.ultimi[:n] if n else c.oggi
        if not lista:
            blocchi.append(f"{_icona(c)} <b>{c.nome}</b>: nessuna operazione"
                           + (" oggi." if not n else " recente."))
            continue
        righe = []
        for t in lista:
            imp = _amount(t)
            ic = "🟢" if imp > 0 else ("🔴" if imp < 0 else "⚪")
            righe.append(f"{ic} {ora_locale(t.get('dateUtc') or '', '%H:%M')}  "
                         f"{nome_strumento(t.get('instrumentName', '?'))}  {eur(imp, True)}")
        somma = sum(_amount(t) for t in lista)
        titolo = f"ultime {len(lista)}" if n else f"{len(lista)} oggi"
        blocchi.append(f"{_icona(c)} <b>{c.nome}</b> ({titolo}, totale {eur(somma, True)})\n"
                       + "\n".join(righe))
    testa = "🧾 <b>Operazioni chiuse</b>" + ("" if n else f" · {data_estesa()}")
    return testa + "\n\n" + "\n\n".join(blocchi)


def messaggio_aiuto() -> str:
    from src.grid_esercizio import leggi
    st = leggi("demo")
    ct = st.get("capitale")
    esercizio = (f"/claudetrade · come va il conto da {eur(ct)}\n"
                 "/pagina · il link al cruscotto sempre aggiornato\n" if ct else "")
    coda_esercizio = (" Il resoconto di ClaudeTrade arriva ogni sera alle 22:30, "
                      "dal lunedì al venerdì." if ct else "")
    return ("ℹ️ <b>Cosa puoi chiedermi</b>\n\n"
            "<b>Guardare</b>\n"
            "/stato · quanto ho e come sta andando, adesso\n"
            + esercizio +
            "/posizioni · le posizioni aperte, spiegate\n"
            "/oggi · le operazioni chiuse oggi (/oggi10 = le ultime 10)\n\n"
            "<b>Intervenire</b>\n"
            "/ferma reale · chiude tutto sul conto reale e lo mette in pausa\n"
            "/ferma prova · lo stesso sull'altro conto\n"
            "/riparti reale · fa ripartire il conto reale dalla cifra attuale\n"
            "/riparti prova · lo stesso sull'altro conto\n\n"
            "<b>Questo elenco</b>\n"
            "/aiuto\n\n"
            "<i>Messaggi automatici: riepilogo ogni ora, buongiorno alle 8, "
            "riepilogo della settimana la domenica sera. Sabato e domenica, a "
            "mercati fermi, il riepilogo orario arriva solo se cambia qualcosa: "
            "restano il buongiorno e la chiusura delle 22:30."
            + coda_esercizio +
            " Gli avvisi importanti arrivano subito.</i>")


# compatibilita' con i vecchi nomi (comandi /stat, /statN)
messaggio_riepilogo = messaggio_stato
messaggio_ultimi = messaggio_oggi


# ------------------------------------------------- silenzio nei giorni fermi

# Sabato e domenica i mercati del grid (indici e oro) sono chiusi: il riepilogo
# orario ripeterebbe gli stessi numeri 24 volte. Regola del 2026-09-05: in quei
# giorni si parla solo se qualcosa e' cambiato davvero.
GIORNI_SILENZIOSI = {5, 6}
SOGLIA_EURO = 0.10          # sotto questa cifra non e' una notizia


def giorno_silenzioso(dt: datetime | None = None) -> bool:
    return (dt or datetime.now(ROMA)).weekday() in GIORNI_SILENZIOSI


def impronta(conti: list[Conto]) -> dict:
    """Fotografia minima di cosa c'e' da raccontare: se questa non cambia, il
    messaggio sarebbe identico al precedente."""
    out = {}
    for c in conti:
        if not c.ok:
            out[c.env] = {"ok": False}
            continue
        out[c.env] = {
            "ok": True,
            "equity": round(c.equity, 2),
            "movimenti": c.movimenti_oggi,
            "bloccato": c.bloccato,
            "posizioni": {p["epic"]: round(p["size"], 4) for p in c.posizioni},
        }
    return out


def cambio_rilevante(prec: dict | None, ora: dict) -> bool:
    """True se rispetto all'ultimo messaggio inviato e' successo qualcosa:
    un'operazione, una posizione aperta o chiusa, il conto fermato o ripartito,
    il broker caduto, oppure un movimento di almeno SOGLIA_EURO."""
    if not prec:
        return True
    if set(prec) != set(ora):
        return True
    for env, adesso in ora.items():
        p = prec[env]
        if p.get("ok") != adesso.get("ok"):
            return True
        if not adesso.get("ok"):
            continue
        if (p.get("movimenti") != adesso.get("movimenti")
                or p.get("bloccato") != adesso.get("bloccato")
                or p.get("posizioni") != adesso.get("posizioni")):
            return True
        if abs(float(p.get("equity", 0)) - adesso["equity"]) >= SOGLIA_EURO:
            return True
    return False
