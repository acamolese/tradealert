"""Riepilogo dell'attivita' dei grid, sui DUE conti insieme.

Serve sia il messaggio orario automatico sia i comandi Telegram su richiesta
(/stat, /stat10, /conti). Fonte dei numeri: sempre il BROKER
(/history/transactions + posizioni + saldo), mai il DB, i cui pnl hanno segno
inaffidabile.

Un conto solo non basta a capire: reale e demo girano con la stessa logica ma
taglie diverse, e il confronto dice se una differenza di risultato viene dalla
strategia o dalla dimensione.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROMA = ZoneInfo("Europe/Rome")


def ora_locale(iso_utc: str, fmt: str = "%d/%m %H:%M") -> str:
    """Converte un timestamp UTC del broker in ora italiana.

    L'API Capital lavora in UTC (verificato il 21/08: le query con orari UTC
    trovano i movimenti, quelle con orari locali no), ma i messaggi li legge una
    persona che guarda l'orologio italiano: mostrarli in UTC ha gia' generato
    confusione. I CONFRONTI restano in UTC, si converte solo per la stampa.
    """
    if not iso_utc:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ROMA).strftime(fmt)
    except ValueError:
        return iso_utc[:16]
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def _amount(t: dict) -> float:
    try:
        return float(t.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Conto:
    nome: str
    env: str
    equity: float = 0.0
    saldo: float = 0.0
    flottante: float = 0.0
    baseline: float | None = None
    baseline_data: str = ""
    equity_giorno: float | None = None
    bloccato: bool = False
    posizioni: list = field(default_factory=list)
    movimenti_ora: int = 0
    movimenti_oggi: int = 0
    realizzato_ora: float = 0.0
    realizzato_oggi: float = 0.0
    costi_oggi: float = 0.0
    ultimi: list = field(default_factory=list)

    @property
    def guadagno(self) -> float:
        return (self.equity - self.baseline) if self.baseline else 0.0

    @property
    def guadagno_oggi(self) -> float:
        """Delta equity da inizio giornata (mezzanotte italiana). E' la risposta
        a "come sto andando OGGI": include il flottante, che il solo realizzato
        nasconde (il grid porta per costruzione inventario momentaneamente
        negativo, e contare solo i gradini chiusi ha gia' generato confusione)."""
        return (self.equity - self.equity_giorno) if self.equity_giorno else 0.0


def _baseline_giorno(env: str, equity: float) -> float:
    """Equity alla PRIMA lettura del giorno (ora italiana), persistita su file:
    tutte le letture successive della giornata confrontano contro quella."""
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


def raccogli(capital, env: str, nome: str, n_ultimi: int = 10) -> Conto:
    """Fotografia di un conto: saldo, posizioni, movimenti e P&L da broker."""
    from jobs.account_truth import fetch_transactions

    c = Conto(nome=nome, env=env)
    acc = (capital.get_account_info().get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}
    c.saldo = float(bal.get("balance") or 0)
    c.flottante = float(bal.get("profitLoss") or 0)
    c.equity = c.saldo + c.flottante

    st = DATA / f"g2_profit_{env}.json"
    if st.exists():
        try:
            d = json.loads(st.read_text())
            c.baseline = float(d.get("baseline") or 0) or None
            c.bloccato = bool(d.get("bloccato"))
            c.baseline_data = ora_locale(d.get("creato") or "", "%d/%m")
        except Exception:
            pass
    c.equity_giorno = _baseline_giorno(env, c.equity)

    # aggregate per strumento: il broker tiene righe separate per ogni ordine
    # nella stessa direzione (il netting scatta solo tra versi opposti), ma cio'
    # che conta per il grid e' la posizione NETTA su ciascun mercato.
    agg: dict[str, dict] = {}
    for p in capital.get_open_positions():
        po, m = p.get("position", {}), p.get("market", {})
        s = float(po.get("size") or 0)
        if (po.get("direction") or "").upper() == "SELL":
            s = -s
        ep = m.get("epic") or "?"
        r = agg.setdefault(ep, {"epic": ep, "size": 0.0, "pnl": 0.0, "righe": 0})
        r["size"] += s
        r["pnl"] += float(po.get("upl") or 0)
        r["righe"] += 1
    c.posizioni = sorted(agg.values(), key=lambda r: r["epic"])

    ora = datetime.now(timezone.utc)
    tx = fetch_transactions(capital, 2)
    lim_ora = (ora - timedelta(hours=1)).isoformat()
    # "oggi" = da mezzanotte ITALIANA (convertita in UTC per confrontare con
    # dateUtc del broker): il giorno UTC inizia alle 02:00 e confondeva i conteggi
    lim_gg = (datetime.now(ROMA).replace(hour=0, minute=0, second=0, microsecond=0)
              .astimezone(timezone.utc).isoformat())
    for t in tx:
        d = t.get("dateUtc") or ""
        tipo = t.get("transactionType")
        imp = _amount(t)
        if d >= lim_gg:
            c.movimenti_oggi += 1 if tipo == "TRADE" else 0
            c.realizzato_oggi += imp if tipo == "TRADE" else 0.0
            if tipo in ("SWAP", "CORPORATE_ACTION"):
                c.costi_oggi += imp
        if d >= lim_ora:
            c.movimenti_ora += 1 if tipo == "TRADE" else 0
            c.realizzato_ora += imp if tipo == "TRADE" else 0.0
    c.ultimi = [t for t in reversed(tx) if t.get("transactionType") == "TRADE"][:n_ultimi]
    return c


def _riga_conto(c: Conto) -> str:
    # il semaforo dice come sta andando OGGI (equity da mezzanotte italiana):
    # e' la domanda che si fa chi legge; il cumulato dall'avvio ha la sua riga
    seg = "🟢" if c.guadagno_oggi >= 0 else "🔴"
    testa = f"{seg} <b>{c.nome}</b>  {c.equity:.2f}€"
    if c.bloccato:
        testa += "  ⏸ IN PAUSA"
    righe = [testa,
             f"   oggi: <b>{c.guadagno_oggi:+.2f}€</b> di equity | "
             f"{c.movimenti_oggi} mosse chiuse {c.realizzato_oggi:+.2f}€ | "
             f"costi {c.costi_oggi:+.2f}€"]
    if c.baseline:
        avvio = f" ({c.baseline_data})" if c.baseline_data else ""
        righe.append(f"   dall'avvio{avvio}: {c.guadagno:+.2f}€")
    righe.append(f"   ultima ora: {c.movimenti_ora} mosse, {c.realizzato_ora:+.2f}€")
    if c.posizioni:
        det = ", ".join(f"{p['epic']} {'+' if p['size'] > 0 else ''}{p['size']:g}"
                        f"{'' if p['righe'] == 1 else f'×{p["righe"]}'} "
                        f"({p['pnl']:+.2f}€)" for p in c.posizioni)
        righe.append(f"   aperte: {det}")
        righe.append(f"   flottante inventario: {c.flottante:+.2f}€ "
                     f"(scorta del grid, normale che sia sotto)")
    else:
        righe.append("   aperte: nessuna")
    return "\n".join(righe)


def messaggio_riepilogo(conti: list[Conto], titolo: str = "Riepilogo orario") -> str:
    tot_mosse = sum(c.movimenti_oggi for c in conti)
    tot_real = sum(c.realizzato_oggi for c in conti)
    tot_eq = sum(c.guadagno_oggi for c in conti)
    corpo = "\n\n".join(_riga_conto(c) for c in conti)
    return (f"📊 <b>{titolo}</b>\n\n{corpo}\n\n"
            f"<i>Oggi sui due conti: equity {tot_eq:+.2f}€ | {tot_mosse} mosse "
            f"chiuse {tot_real:+.2f}€ realizzati.</i>")


def messaggio_ultimi(conti: list[Conto], n: int) -> str:
    blocchi = []
    for c in conti:
        if not c.ultimi:
            blocchi.append(f"<b>{c.nome}</b>: nessun movimento recente")
            continue
        righe = []
        for t in c.ultimi[:n]:
            q = ora_locale(t.get("dateUtc") or "")
            imp = _amount(t)
            ic = "🟢" if imp >= 0 else "🔴"
            righe.append(f"{ic} {q}  {t.get('instrumentName', '?'):<9} {imp:+.2f}€")
        somma = sum(_amount(t) for t in c.ultimi[:n])
        blocchi.append(f"<b>{c.nome}</b> (ultimi {len(c.ultimi[:n])}, "
                       f"totale {somma:+.2f}€)\n" + "\n".join(righe))
    return (f"🧾 <b>Ultimi movimenti</b> <i>(ora italiana)</i>\n\n"
            + "\n\n".join(blocchi))
