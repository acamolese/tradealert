# Sprint 7 M4 — Walk-forward strategie senza LLM

Data: 2026-07-02. Script: `jobs/backtest_run.py`. Simulatore validato in M3
(`docs/`, gate passato). Dati: candele HOUR 2020-2026, 5 asset core, spread
reale bid/ask, overnight 0.025%/notte (0.06% BTC), cooldown 24h, uscite =
sistema live (SL k*ATR, TP rr*SL, trailing D+V1+V2).

## Round 1 (2026-07-02) — VERDETTO: NESSUNA CANDIDATA PASSA

Protocollo: selezione in-sample 2020-2023, giudizio out-of-sample 2024-oggi.
Gate: exp OOS ≥ +0.20R netto, n≥60, senza top2 ≥ +0.10R, 2/3 terzi positivi.

| Famiglia | Config scelta IS | exp IS | exp OOS | n OOS | Gate |
|---|---|---|---|---|---|
| donchian | N=40, SL 2.5 ATR, rr 3 | -0.050R | **+0.121R** | 407 | NO |
| emacross | 10/40, SL 1.5, rr 3 | +0.015R | +0.007R | 115 | NO |
| tsmom | K=20, SL 2.5, rr 2 | -0.038R | -0.005R | 1967 | NO |

**Il fatto centrale e scomodo: TUTTE le 28 configurazioni sono negative o
nulle in-sample (2020-2023).** Non esiste una candidata legittima: la
selezione IS ha solo scelto la "meno peggio" per famiglia.

Il dato che tenta, e perché NON va comprato: donchian 40gg fa +0.121R su 407
trade OOS, 3/3 sottoperiodi positivi, Gold +0.42R, long +0.17R. Ma era
negativa nei 4 anni precedenti: a fine 2023 nessun processo onesto l'avrebbe
scelta. È la firma classica della regime-dipendenza del trend-following: il
2024-2026 è stato un regime di trend forti (oro su tutti), il 2020-2023 no.
Implicazione collaterale importante: anche i profitti live di TradeAlert 2026
(runner su Gold/Brent) sono coerenti con "regime favorevole", non con edge
del selettore.

## Round 2 — PRE-REGISTRATO prima di eseguire (2026-07-02)

Due estensioni note in letteratura come le correzioni giuste per il
trend-following, con un criterio PIÙ severo per compensare il rischio di
confronti multipli (secondo giro di test sugli stessi dati):

**Criterio round 2 (vale per entrambe le estensioni):** una candidata è
valida SOLO se exp IS > 0 (config profittevole anche in-sample) E il gate
OOS di round 1 è rispettato (exp ≥ +0.20R, n≥60, senza top2 ≥ +0.10R,
2/3 terzi positivi).

**2a — Barre giornaliere, storia lunga (dati già scaricati):** DAY 2015-2026
sui 5 core. IS 2015-2022 (8 anni, include 2 regimi ribassisti), OOS
2023-2026 (3.5 anni). Stesse famiglie e stessa griglia. Caveat dichiarato:
la risoluzione daily delle uscite non è validata M3 (l'orario esiste solo dal
2020); se emerge una candidata, la sua porzione 2020-2026 va ri-simulata a
candele orarie prima di crederci.

**2b — Universo largo, orario:** 15-25 asset liquidi aggiuntivi (metalli,
energy, indici EU/Asia, FX major, ETH), stesso protocollo IS/OOS di round 1.
La diversificazione è LA correzione standard per la regime-dipendenza del
trend-following (i CTA reali girano su 50+ mercati). Richiede download
(~30-60 min).

**Se anche il round 2 non produce candidate:** l'ipotesi "motore
deterministico trend-following su questo broker/orizzonte" si archivia come
NEGATIVA, e restano le opzioni C (finestra 50 trade del sistema attuale) e
D (spegnere) del documento strategico. Non si fanno round 3 di data mining.

## Round 2 — ESEGUITO 2026-07-02: VERDETTO FINALE NEGATIVO

**2a (DAY 2015-2026, IS 2015-2022 / OOS 2023-2026):** config IS-positive
esistono (migliori: donchian N=20 +0.110R su 737 trade in 8 anni, tsmom K=20
+0.092R), ma TUTTE decadono OOS: donchian -0.003R, emacross +0.035R (non
robusto: senza top2 -0.003R), tsmom +0.052R (1/3 terzi positivi). Nessuna
passa. Costo overnight sul daily doppio dell'orario (~0.09R/trade). Pattern
ricorrente OOS: long +0.14/+0.18R, short -0.12/-0.25R (il 2023-2026 premia
solo il lato long dei trend).

**2b (HOUR 2020-2026, 29 mercati: metalli, energy, indici US/EU/Asia, FX
major, crypto):** la diversificazione PEGGIORA il quadro. Tutte le 28
configurazioni negative in-sample su tutte le famiglie (da -0.05 a -0.14R;
tsmom su FX il peggiore, fino a -3148R totali su 22k trade). Col vincolo
pre-registrato IS>0, nessuna config è nemmeno eleggibile al giudizio OOS.
La lettura: ai costi retail CFD (spread + overnight) e su orizzonte
orario/swing, il trend-following classico non sopravvive; sui mercati FX
lo spread mangia tutto in rapporto all'ATR.

## Verdetto M4 complessivo: ARCHIVIATO NEGATIVO

Tre famiglie × due timeframe × 29 mercati × walk-forward onesto = nessuna
candidata. Come da pre-registrazione: **niente round 3**. L'opzione A del
documento strategico (motore deterministico) è FALSIFICATA nel perimetro
testato. Restano: opzione C (gate 50 trade del sistema live, già in corso,
agenda attiva) e opzione D (spegnere/ridurre).

Nota metodologica finale: questo risultato, ottenuto in poche ore di calcolo,
avrebbe richiesto anni di forward test. Il valore dell'harness resta: ogni
futura idea di strategia si può falsificare in minuti prima di toccare
produzione (`jobs/backtest_run.py`, dati in `data/candles/`, 29 mercati).
