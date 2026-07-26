# Sprint 9 — Protezione del profitto sul controller v2 (pre-registrato)

Data: 2026-07-26. Stato: **PRE-REGISTRATO prima di calcolare gli esiti.**
Soglie e predizione fissate ORA (anti-HARKing).

## Origine

Intuizione utente: v2 tiene l'esposizione e non prende mai profitto; in un giorno
buono il guadagno e' flottante e, se il mercato torna indietro *lentamente* (bassa
volatilita', il controller non reagisce), quel guadagno evapora. Proposta: un
meccanismo che protegga almeno parzialmente il guadagnato (trailing/lock).

Limite reale confermato: il controller v2 de-riska solo sulla VOLATILITA', quindi
NON copre un ritracciamento lento e tranquillo. La domanda e' se un trailing lo
copre senza costare piu' di quanto salva.

## Cosa si testa

Backtest US500 daily (2020-2026). Esposizione base = volatility targeting del
controller: `w = clip(SIGMA_TARGET/sigma_ewma, 0, MAX_SCALE)`. Equity in compounding
di `w*ret - costi` (financing 0.025%/die sull'esposizione + spread sul turnover).

Varianti (stessi dati, stessi costi):
- **puro**: solo controller (nessuna protezione). Il riferimento.
- **trail-3% / trail-5% / trail-8%**: se US500 ritraccia dal suo picco (dall'ultimo
  ingresso) oltre la frazione, si va FLAT (w=0); si rientra quando il prezzo recupera
  un nuovo massimo. Questo e' il "porta avanti il guadagnato" dell'utente.
- **always** (w=1 costante): il beta passivo, come nel benchmark ombra.

Metriche: rendimento cumulato netto, **max drawdown**, **Sharpe**, giorni fuori
mercato (proxy del churn/costo-opportunita').

## Gate PRE-REGISTRATO

| Verdetto | Condizione |
|---|---|
| **PROTEZIONE UTILE** | almeno una variante trail ha **Sharpe ≥ puro** E max drawdown migliore (meno profondo) di ≥ 20% relativo. Protegge senza erodere il rischio-aggiustato → si valuta il deploy pre-registrato. |
| **PROTEZIONE DANNOSA** | tutte le trail hanno rendimento cum < puro **e** Sharpe ≤ puro. Taglia i ritracci normali dell'S&P che poi risale → non si mette. |
| **TRADE-OFF** | riduce il drawdown ma erode il rendimento (Sharpe ~uguale). Non decide il gate: e' una scelta di tolleranza al rischio dell'utente, esplicitata coi numeri. |

## Predizione PRE-REGISTRATA

Sull'S&P (deriva verso l'alto, ritracci del 3-5% frequenti e normali), mi aspetto
che il trailing **eroda il rendimento** (esci nei ritracci fisiologici, il mercato
risale senza di te) piu' di quanto riduca il drawdown, con Sharpe ≤ puro →
PROTEZIONE DANNOSA o TRADE-OFF sfavorevole. Coerente col trailing calibration
(artefatto) e con l'uscita a target fisso (tagliava i winner). Ma testabile: mi
sono gia' sbagliato indovinando invece di misurare.

Sola lettura. Script `jobs/profit_lock_test.py`.

## ESEGUITO 2026-07-26 — PROTEZIONE DANNOSA (netto)

US500 daily 2020-2026 (3552 barre).

| variante | rend. cum | Sharpe | max DD | gg flat |
|---|---|---|---|---|
| **puro** (controller) | **+32.5%** | **0.22** | **-29.6%** | 0 |
| trail-3% | -13.5% | -0.06 | -33.8% | 1952 |
| trail-5% | -22.6% | -0.11 | -39.7% | 1644 |
| trail-8% | -16.0% | -0.05 | -36.0% | 1408 |
| always (w=1) | +63.3% | 0.31 | -33.8% | 0 |

Il trailing e' il **peggio dei due mondi**: (1) erode il rendimento da +32.5% a
NEGATIVO (-13/-22%); (2) NON riduce il drawdown, lo **peggiora** (-34/-40% vs -30%).
Passa meta' del tempo fuori mercato (1952/3300 gg).

**Meccanismo (importante, controintuitivo):** sull'S&P che deriva verso l'alto con
ritracci frequenti del 3-5%, il trailing esce a ogni ritraccio normale e RIENTRA su
un nuovo massimo, cioe' compra piu' in alto. Al ritraccio successivo riesci piu' in
basso. Vendi basso, compri alto, ripetutamente: erode il capitale E accumula
drawdown dai nuovi punti d'ingresso alti, invece di ridurli. La protezione del
profitto via trailing, su un asset trending, distrugge sia rendimento sia difesa.

**Verdetto: NON mettere.** Il controller puro batte tutte le varianti protette.
Predizione pre-registrata (trailing dannoso) CONFERMATA. Se si vuole davvero
tutelare un guadagno specifico, l'unica via sana e' la chiusura MANUALE discrezionale
(l'utente decide di incassare), non una regola automatica di trailing.
