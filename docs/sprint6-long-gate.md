# Sprint 6 A2 — Gate direzionale lato long

Data esecuzione: 2026-07-02. Script: `jobs/long_gate_analysis.py` (sola lettura).
Gate pre-registrato in `docs/sprint6-piano-scalata.md`. Dipende dall'output A1
(`docs/sprint6-monitor-replay.csv`) per il controfattuale bracket-only dei trade
chiusi dal monitor.

## Verdetto: NON CONCLUSIVO → nessuna azione, ripetere a +15 long chiusi (n=45)

| Condizione LONG DA BLOCCARE | Soglia | Osservato | Esito |
|---|---|---|---|
| exp_R long reale | ≤ -0.10R | -0.134R | sì |
| exp_R long controfattuale | ≤ -0.10R | -0.124R | sì |
| n | ≥ 30 | 30 | sì |
| robustezza (a): senza i 2 long peggiori | ≤ -0.10R | **-0.073R / -0.061R** | **no** |
| robustezza (b): solo 5 asset core | ≤ -0.10R | -0.105R / **-0.087R** | **no** |

Il bleed long esiste in media ma NON è robusto: togliendo 2 trade o restringendo
al core scende sotto la soglia di significatività pratica. Per la regola di
metodo (lezione #42/#49), un verdetto che si rovescia togliendo 2 trade non è
un verdetto.

## Numeri (trade chiusi dal 2026-05-20, n=64, 0 esclusi)

| Taglio | n | exp_R reale | exp_R controfattuale |
|---|---|---|---|
| LONG | 30 | -0.134 | -0.124 |
| SHORT | 34 | +0.257 | +0.161 |
| LONG senza i 2 peggiori | 28 | -0.073 | -0.061 |
| LONG solo core | 27 | -0.105 | -0.087 |

Per asset (long, reale): Gold -0.351 (n=8), Brent -0.313 (n=6), Copper -0.108
(n=2), GBP/USD -0.972 (n=1), Nasdaq +0.062 (n=10), Bitcoin +0.425 (n=2),
US500 +0.377 (n=1).

## Lettura

1. Il ramo "PROBLEMA DI GESTIONE" è escluso: il controfattuale bracket-only è
   negativo quanto il reale (-0.124R vs -0.134R). Se c'è un problema long, è di
   selezione, non del monitor. Coerente con l'esito A1.
2. Il bleed è concentrato in **Gold long e Brent long** (14 trade, exp ≈ -0.33R),
   mentre Nasdaq long è neutro-positivo su n=10. Campioni troppo piccoli per un
   gate per-asset oggi; è l'ipotesi da guardare alla ripetizione.
3. Lo short conferma l'edge anche nel controfattuale (+0.161R): non è un
   artefatto della gestione.

## Azione (da matrice pre-registrata)

Nessuna modifica a produzione. `LONG_RISK_FACTOR` NON viene introdotto.
**Trigger di ripetizione: +15 long chiusi** (oggi n=30 → ripetere a n≥45,
stesso script, stesse soglie). Alla ripetizione, aggiungere il breakdown
pre-registrato ora: exp_R di Gold+Brent long vs resto dei long.

---

# ROUND 2 — 2026-07-12 (trigger agenda: n=45 long chiusi)

Controfattuale A1 rigenerato lo stesso giorno (66 chiusure monitor simulate,
4 scartate, verdetto A1 sempre NEUTRO; riapertura formale A1 resta a n≥81).

## Verdetto: NON CONCLUSIVO → nessuna azione, ripetere a n≥60 long

| Condizione LONG DA BLOCCARE | Soglia | Osservato | Esito |
|---|---|---|---|
| exp_R long reale | ≤ -0.10R | **-0.065R** | **no** |
| exp_R long controfattuale | ≤ -0.10R | **-0.052R** | **no** |
| n | ≥ 30 | 45 | sì |

Il gate fallisce già sulla condizione principale: il bleed medio si è più che
dimezzato rispetto al round 1 (-0.134R → -0.065R) e senza i 2 peggiori è
-0.022R. Nessun ramo della matrice scatta.

## Numeri (trade chiusi dal 2026-05-20, n=81, 0 esclusi)

| Taglio | n | exp_R reale | exp_R controfattuale |
|---|---|---|---|
| LONG | 45 | -0.065 | -0.052 |
| SHORT | 36 | +0.233 | +0.116 |
| LONG senza i 2 peggiori | 43 | -0.022 | -0.008 |
| LONG solo core | 37 | -0.042 | -0.026 |
| **Gold+Brent long (breakdown pre-reg.)** | 18 | **-0.138** | -0.311 |
| **resto dei long** | 27 | **-0.017** | +0.121 |

Per asset (long, reale): Gold -0.244 (n=10), Copper -0.234 (n=4),
GBP/USD -0.601 (n=2), Brent -0.006 (n=8), Nasdaq -0.003 (n=11),
Bitcoin +0.122 (n=5), US500 +0.118 (n=3), Hang Seng +0.375 (n=2).

## Lettura

1. La tendenza è verso la normalizzazione: i 15 long di luglio hanno smesso
   di sanguinare e Brent long è rientrato da -0.313R a -0.006R.
2. Il residuo negativo è concentrato in **Gold long** (n=10, -0.244R), ma
   n=10 non regge un gate per-asset: resta l'ipotesi da guardare al round 3.
3. Lo short conferma l'edge (+0.233R reale, +0.116R controfattuale).

## Azione (da matrice pre-registrata)

Nessuna modifica. Round 3 a **n≥60 long chiusi** (agenda aggiornata,
`A2_TARGET_LONGS=60`), con lo stesso breakdown e attenzione a Gold long.

---

# ROUND 3 — 2026-07-16 (trigger agenda: n=61 long chiusi)

Controfattuale A1 NON rigenerato: `docs/sprint6-monitor-replay.csv` è fermo al
2026-07-12 (66 chiusure monitor). Le 18 chiusure nuove (di cui 14
`manual:auto-close LLM monitor`, 12 long) non hanno il bracket-only e usano
cf=reale. Nota di rigore, non incide sul verdetto: il gate cade già sul ramo
reale (soglia sul reale non raggiunta), e il ramo controfattuale/gestione non è
raggiungibile con reale > −0.10R.

## Verdetto: NON CONCLUSIVO → nessuna azione, ripetere a n≥76 long

| Condizione LONG DA BLOCCARE | Soglia | Osservato | Esito |
|---|---|---|---|
| exp_R long reale | ≤ -0.10R | **-0.040R** | **no** |
| exp_R long controfattuale | ≤ -0.10R | **-0.031R** | **no** |
| n | ≥ 30 | 61 | sì |

Terzo round consecutivo NON CONCLUSIVO. Il bleed medio continua a normalizzarsi
in modo monotòno round su round (-0.134R → -0.065R → **-0.040R**) e senza i 2
peggiori è a -0.008R: il gate si allontana dal fuoco, non ci si avvicina.

## Numeri (trade chiusi dal 2026-05-20, n=99, 0 scartati)

| Taglio | n | exp_R reale | exp_R controfattuale |
|---|---|---|---|
| LONG | 61 | -0.040 | -0.031 |
| SHORT | 38 | +0.185 | +0.074 |
| LONG senza i 2 peggiori | 59 | -0.008 | +0.002 |
| LONG solo core | 44 | -0.032 | -0.018 |
| **Gold+Brent long (breakdown pre-reg.)** | 21 | **-0.102** | -0.250 |
| **resto dei long** | 40 | **-0.008** | +0.085 |

Per asset (long, reale): Gold -0.266 (n=11), Copper -0.246 (n=6),
GBP/USD -0.368 (n=3), EUR/USD -0.550 (n=1), Brent +0.079 (n=10),
Nasdaq -0.011 (n=12), Bitcoin +0.099 (n=7), Hang Seng +0.192 (n=5),
US500 +0.045 (n=4), AUD/USD +0.548 (n=2).

## Lettura

1. Nessun ramo della matrice scatta. Il long non blocca e non è problema di
   gestione: semplicemente non sanguina più in aggregato.
2. Brent long ha invertito segno (round 1 -0.313R → round 3 +0.079R). Il
   breakdown Gold+Brent long è a -0.102R (n=21) solo perché tenuto giù da Gold;
   è la sonda pre-registrata, non un gate, e n resta piccolo per un blocco
   per-asset.
3. Il residuo negativo resta concentrato in **Gold long** (n=11, -0.266R) e in
   Copper long (n=6, -0.246R, nuovo asset di giugno, materia di A3). Gold è
   l'unica ipotesi ancora viva ma non regge da sola un gate.
4. Lo short conferma l'edge (+0.185R reale, +0.074R controfattuale).

## Azione (da matrice pre-registrata)

Nessuna modifica. Per la regola NON CONCLUSIVO il prossimo trigger è **+15 long
chiusi → n≥76** (agenda da riarmare a `A2_TARGET_LONGS=76`), stesso script e
stesso breakdown.

Nota di metodo per l'utente (fuori matrice): il bleed long si è normalizzato su
tre round e il gate si allontana dal fuoco. Ripetere a oltranza ogni +15 ha
rendimenti decrescenti; è ragionevole valutare la chiusura di A2 con esito
"nessun bleed long robusto, normalizzato" invece di un quarto round. Decisione
dell'utente, non automatica.
