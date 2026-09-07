"""Quattro idee mai provate su questo progetto, misurate con lo stesso metro.

Richiesta dell'utente (2026-09-07): "non voglio che entri short o long, voglio un
sistema che compra e vende in base ai dati; dobbiamo osare e testare anche
soluzioni fantasiose". Tutte le ipotesi classiche sono state falsificate:
tempismo, regime, frequenza, punteggio LLM, motore deterministico, momentum. Qui
si esce dal solco: nessuna di queste quattro prova a prevedere se un mercato
salira'; sfruttano struttura, non direzione.

  1. STAFFETTA  I mercati chiudono in ore diverse. Mentre Tokyo dorme, New York
     si muove. Alla riapertura, il mercato addormentato recupera quel movimento?
     Se si', il segnale e' gia' noto prima che l'operazione parta.

  2. GEMELLI    US500 e US100 sono quasi la stessa cosa. Quando uno si allontana
     dall'altro, la distanza rientra? Comprare l'uno e vendere l'altro non ha
     direzione: guadagna se la coppia si ricompone, qualunque cosa faccia il
     mercato.

  3. OROLOGIO   Le ore della giornata non sono uguali: apertura, chiusura, notte.
     Esiste un'ora che rende sistematicamente, a prescindere dal resto?

  4. MOLLA      Dopo un periodo di calma piatta arriva uno strappo (la volatilita'
     torna verso la sua media). Si puo' entrare prima dello strappo?

Per ciascuna: l'edge in percento, la coerenza (un effetto vero non cambia segno
tra strumenti e periodi) e il confronto con il costo reale di un'operazione.

Uso: python -m jobs.idee_nuove [--min-edge 0.02]
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
EPICS = ["US100", "US30", "US500", "DE40", "NL25", "J225", "HK50", "GOLD"]
SPREAD = {"US100": 0.0061, "US30": 0.0038, "US500": 0.0078, "DE40": 0.0058,
          "NL25": 0.0090, "J225": 0.0152, "HK50": 0.0197, "GOLD": 0.0114}


def serie(ep: str) -> list[tuple[datetime, float]]:
    f = CACHE / f"15m_{ep}.json"
    if not f.exists():
        return []
    out = []
    for b in json.loads(f.read_text())["barre"]:
        try:
            out.append((datetime.fromisoformat(b["t"]), (b["bid"] + b["ask"]) / 2))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def statistica(v: list[float]) -> tuple[float, float, int]:
    """Media, errore standard, quante osservazioni. Il rapporto media/errore
    dice se il numero e' distinguibile dal caso (oltre 2 e' un indizio serio)."""
    n = len(v)
    if n < 30:
        return 0.0, 0.0, n
    m = statistics.mean(v)
    sd = statistics.stdev(v)
    return m, (sd / (n ** 0.5)) if sd else 0.0, n


def riga(nome: str, v: list[float], costo: float) -> str:
    m, se, n = statistica(v)
    if n < 30:
        return f"  {nome:34s} {'pochi dati':>12s}"
    t = m / se if se else 0.0
    verdetto = "NIENTE"
    if abs(t) >= 2 and abs(m) > costo:
        verdetto = "da guardare" if abs(t) < 3 else "SEGNALE"
    return (f"  {nome:34s} {m:+9.4f}% {se:8.4f} {t:+6.1f} {n:7d} "
            f"{costo:7.4f}% {verdetto:>12s}")


TESTA = (f"  {'':34s} {'edge':>9s} {'errore':>8s} {'t':>6s} {'n':>7s} "
         f"{'costo':>8s} {'':>12s}")


def test_staffetta(dati: dict) -> None:
    """Il movimento di un mercato mentre l'altro e' chiuso predice la sua riapertura?

    Si prende il rendimento di GUIDA nell'intervallo in cui SEGUE non ha barre,
    e si guarda il primo movimento di SEGUE alla riapertura.
    """
    print("\n1. STAFFETTA — chi apre dopo recupera il movimento di chi era aperto?")
    print(TESTA)
    coppie = [("US500", "J225"), ("US500", "HK50"), ("US500", "DE40"),
              ("US100", "J225"), ("US100", "DE40"), ("DE40", "US500"),
              ("J225", "HK50"), ("US500", "NL25")]
    for guida, segue in coppie:
        g, s = dati.get(guida), dati.get(segue)
        if not g or not s:
            continue
        gd = {t: p for t, p in g}
        risultati = []
        for i in range(1, len(s)):
            t_prec, p_prec = s[i - 1]
            t_ora, p_ora = s[i]
            salto = (t_ora - t_prec).total_seconds() / 3600
            if salto < 2:                        # non e' una riapertura
                continue
            # quanto ha fatto la guida durante la chiusura
            finestra = [(t, p) for t, p in g if t_prec <= t <= t_ora]
            if len(finestra) < 4:
                continue
            mossa_guida = (finestra[-1][1] / finestra[0][1] - 1) * 100
            if abs(mossa_guida) < 0.1:           # troppo piccola per contare
                continue
            # quanto fa chi riapre, nella prima ora
            fine = min(i + 4, len(s) - 1)
            reazione = (s[fine][1] / p_ora - 1) * 100
            # edge = seguire la direzione della guida
            risultati.append(reazione if mossa_guida > 0 else -reazione)
        print(riga(f"{guida} guida -> {segue} riapre", risultati, SPREAD.get(segue, 0.02)))


def test_gemelli(dati: dict) -> None:
    """Due indici quasi identici che divergono: la distanza rientra?"""
    print("\n2. GEMELLI — la divergenza tra indici parenti si richiude?")
    print(TESTA)
    coppie = [("US500", "US100"), ("US500", "US30"), ("US100", "US30"),
              ("DE40", "NL25"), ("DE40", "US500"), ("NL25", "US500"),
              ("J225", "HK50"), ("GOLD", "US500")]
    for a, b in coppie:
        sa, sb = dati.get(a), dati.get(b)
        if not sa or not sb:
            continue
        da = {t: p for t, p in sa}
        comune = [(t, da[t], p) for t, p in sb if t in da]
        if len(comune) < 500:
            continue
        L, F = 8, 8                              # due ore indietro, due avanti
        risultati = []
        for i in range(L, len(comune) - F):
            t0, a0, b0 = comune[i - L]
            t1, a1, b1 = comune[i]
            t2, a2, b2 = comune[i + F]
            if min(a0, b0, a1, b1) <= 0:
                continue
            # divergenza recente: quanto A ha fatto piu' di B
            div = (a1 / a0 - 1) - (b1 / b0 - 1)
            if abs(div) < 0.001:                 # sotto lo 0,1% e' rumore
                continue
            # rientro: quanto B recupera su A nelle due ore successive
            futuro = ((b2 / b1 - 1) - (a2 / a1 - 1)) * 100
            risultati.append(futuro if div > 0 else -futuro)
        costo = SPREAD.get(a, 0.02) + SPREAD.get(b, 0.02)   # due gambe
        print(riga(f"{a} vs {b}", risultati, costo))


def test_orologio(dati: dict) -> None:
    """Esistono ore della giornata sistematicamente positive o negative?"""
    print("\n3. OROLOGIO — ci sono ore che rendono sempre allo stesso modo?")
    print(TESTA)
    for ep in EPICS:
        s = dati.get(ep)
        if not s:
            continue
        per_ora = defaultdict(list)
        for i in range(1, len(s)):
            t, p = s[i]
            t0, p0 = s[i - 1]
            if (t - t0).total_seconds() > 3600 or p0 <= 0:
                continue
            per_ora[t.hour].append((p / p0 - 1) * 100)
        migliore = None
        for ora, v in per_ora.items():
            m, se, n = statistica(v)
            if n < 100 or not se:
                continue
            t_stat = m / se
            if migliore is None or abs(t_stat) > abs(migliore[1]):
                migliore = (ora, t_stat, m, n)
        if migliore:
            ora, t_stat, m, n = migliore
            print(riga(f"{ep}: ora piu' marcata ({ora:02d}:00 UTC)",
                       per_ora[ora], SPREAD.get(ep, 0.02)))


def test_molla(dati: dict) -> None:
    """Dopo la calma piatta arriva lo strappo: si puo' entrare prima?"""
    print("\n4. MOLLA — dopo un periodo di calma il prezzo si muove di piu'?")
    print(TESTA)
    for ep in EPICS:
        s = dati.get(ep)
        if not s:
            continue
        L, F = 16, 8
        ampiezze_dopo_calma, ampiezze_dopo_agitazione = [], []
        vol = []
        for i in range(L, len(s) - F):
            fin = [p for _, p in s[i - L:i]]
            if min(fin) <= 0:
                continue
            r = [abs(fin[j] / fin[j - 1] - 1) for j in range(1, len(fin))]
            vol.append((i, statistics.mean(r) * 100))
        if len(vol) < 500:
            continue
        soglia_bassa = statistics.quantiles([v for _, v in vol], n=5)[0]
        soglia_alta = statistics.quantiles([v for _, v in vol], n=5)[3]
        for i, v in vol:
            futuro = abs(s[i + F][1] / s[i][1] - 1) * 100
            if v <= soglia_bassa:
                ampiezze_dopo_calma.append(futuro)
            elif v >= soglia_alta:
                ampiezze_dopo_agitazione.append(futuro)
        mc, _, nc = statistica(ampiezze_dopo_calma)
        ma, _, na = statistica(ampiezze_dopo_agitazione)
        if nc and na:
            print(f"  {ep + ': movimento dopo calma / dopo agitazione':34s} "
                  f"{mc:+9.4f}% {ma:+8.4f}% {'rapporto ' + format(mc/ma if ma else 0, '.2f'):>26s}")


def main() -> int:
    dati = {ep: serie(ep) for ep in EPICS}
    dati = {k: v for k, v in dati.items() if v}
    print(f"dati: {len(dati)} strumenti, "
          f"{sum(len(v) for v in dati.values())} barre da 15 minuti")
    print("t = quante volte l'edge supera il proprio errore: sotto 2 e' caso,")
    print("sopra 3 vale la pena guardarci dentro. 'costo' = spread da battere.")
    test_staffetta(dati)
    test_gemelli(dati)
    test_orologio(dati)
    test_molla(dati)
    return 0


if __name__ == "__main__":
    sys.exit(main())
