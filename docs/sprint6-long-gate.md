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
