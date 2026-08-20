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
        except Exception:
            pass

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
    lim_gg = ora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
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
    seg = "🟢" if c.guadagno >= 0 else "🔴"
    testa = (f"{seg} <b>{c.nome}</b>  {c.equity:.2f}€")
    if c.baseline:
        testa += f"  ({c.guadagno:+.2f}€ dall'avvio)"
    if c.bloccato:
        testa += "  ⏸ IN PAUSA"
    righe = [testa,
             f"   ultima ora: {c.movimenti_ora} mosse, {c.realizzato_ora:+.2f}€",
             f"   oggi: {c.movimenti_oggi} mosse, {c.realizzato_oggi:+.2f}€ "
             f"(costi {c.costi_oggi:+.2f}€)"]
    if c.posizioni:
        det = ", ".join(f"{p['epic']} {'+' if p['size'] > 0 else ''}{p['size']:g}"
                        f"{'' if p['righe'] == 1 else f'×{p["righe"]}'} "
                        f"({p['pnl']:+.2f}€)" for p in c.posizioni)
        righe.append(f"   aperte: {det}")
    else:
        righe.append("   aperte: nessuna")
    return "\n".join(righe)


def messaggio_riepilogo(conti: list[Conto], titolo: str = "Riepilogo orario") -> str:
    tot_mosse = sum(c.movimenti_oggi for c in conti)
    tot_real = sum(c.realizzato_oggi for c in conti)
    corpo = "\n\n".join(_riga_conto(c) for c in conti)
    return (f"📊 <b>{titolo}</b>\n\n{corpo}\n\n"
            f"<i>Totale oggi: {tot_mosse} mosse, {tot_real:+.2f}€ realizzati "
            f"sui due conti.</i>")


def messaggio_ultimi(conti: list[Conto], n: int) -> str:
    blocchi = []
    for c in conti:
        if not c.ultimi:
            blocchi.append(f"<b>{c.nome}</b>: nessun movimento recente")
            continue
        righe = []
        for t in c.ultimi[:n]:
            q = (t.get("dateUtc") or "")[5:16].replace("T", " ")
            imp = _amount(t)
            ic = "🟢" if imp >= 0 else "🔴"
            righe.append(f"{ic} {q}  {t.get('instrumentName', '?'):<9} {imp:+.2f}€")
        somma = sum(_amount(t) for t in c.ultimi[:n])
        blocchi.append(f"<b>{c.nome}</b> (ultimi {len(c.ultimi[:n])}, "
                       f"totale {somma:+.2f}€)\n" + "\n".join(righe))
    return f"🧾 <b>Ultimi movimenti</b>\n\n" + "\n\n".join(blocchi)
