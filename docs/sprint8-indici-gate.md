# Sprint 8 — Gate forward del paniere INDICI (pre-registrato)

Data: 2026-07-25. Stato: **PRE-REGISTRATO prima dei trade forward.**
Soglie e predizione fissate ORA, non si spostano dopo aver visto i numeri (anti-HARKing).

## Contesto

Il 2026-07-24 il paniere e' stato ridisegnato ai soli INDICI (US500, Nasdaq/US100,
DE40/DAX, US30/Dow, Gold) + soglia `MIN_SCORE_THRESHOLD=7.2`, dopo che backtest
lungo e trade reali hanno indicato gli indici come gli unici asset con edge
momentum (`docs/sprint8-trailing-revalidation.md`, `basket_composition_result`).
Validazione finora SOLO su backtest (no monitor) e trade reali storici: serve la
prova forward, col sistema nella configurazione finale.

## Metrica e campione

- Popolazione: trade chiusi **dal 2026-07-25** (per costruzione tutti su asset del
  paniere indici; il paniere non contiene altro). Normalizzati in **R** (i pnl in
  euro del DB sono inaffidabili, [[pnl-db-bug]]).
- `exp_R_fwd` = media di `exit_R` reale (da close_price/entry/stop del signal).
- Trigger primario: **>=25 trade indici chiusi**. Fallback temporale: **2026-10-15**
  (se a quella data n<15, campione insufficiente -> estendere, non giudicare).

## Baseline (riferimenti, misurati PRIMA e fissati qui)

- Paniere misto completo pre-ridisegno (2026-05-20 -> 07-24, n=129): **+0.030R**.
- Solo asset-indice nel misto (US500/Nasdaq/Gold, soglia 7.0, n=50): **+0.046R**.
- Predizione backtest indici @7.2 (no monitor): **+0.052R** (OOS +0.118).

## Predizione PRE-REGISTRATA

Il ridisegno (concentrazione sui soli indici + soglia 7.2 + monitor LLM sull'uscita,
che il backtest non cattura) dovrebbe portare `exp_R_fwd` **>= +0.05R** e comunque
**sopra la baseline misto (+0.030R)**. Il monitor dovrebbe far battere il backtest
bracket-only, quindi l'esito reale atteso e' >= +0.05R, plausibilmente di piu'.

## Gate PRE-REGISTRATO

| Verdetto | Condizione (a n>=25) | Azione |
|---|---|---|
| **RIDISEGNO CONFERMATO** | `exp_R_fwd >= +0.05R` **e** positivo **e** robusto (senza i 2 migliori trade >= 0) | Tieni il paniere indici. Valuta se rialzare gradualmente il sizing (era stato ridotto per de-risk). |
| **RIDISEGNO FALLITO** | `exp_R_fwd <= -0.05R` | Peggio della baseline misto: il ridisegno non ha pagato. Rollback (`cp .env.bak-pre-indici .env` + riattiva vecchio UNIVERSE), oppure riconsiderare Bitcoin (in LEGACY_KNOWN, ottimo nei reali). |
| **NON CONCLUSIVO** | `-0.05R < exp_R_fwd < +0.05R` | Nessuna azione, ripetere a +15 trade (n>=40). |

Nota soglie: il campione indici sara' piccolo e ad alta varianza (pochi asset).
+0.05R e' la soglia di conferma allineata a backtest e baseline-indice; -0.05R
segna il "peggio del misto" che giustifica il ripensamento. La zona centrale non
decide: si prosegue.

## Metriche SECONDARIE (monitoraggio a mano, NON gate)

1. **Tasso di apertura**: con 5 asset (da 11) e soglia 7.2 (da 7.0) le aperture
   caleranno molto. Va bene finche' > 0; se lo scanner va a ~0 aperture/settimana
   il sistema e' di fatto fermo -> segnalare (eventualmente riabbassare a 7.0, che
   sul paniere-indici resta positivo +0.041R OOS, o riallargare di un asset).
2. **Primi trade DE40 e US30** (mai tradati): osservare sizing reale e gap
   all'apertura, come fu necessario per il Nikkei (rischio nuovo-asset).

## Caveat metodologici (dichiarati)

- Confronto forward-indici vs baseline-storica-misto NON pareggiato: regime di
  mercato diverso, nessuno shadow parallelo del paniere misto. E' il meglio
  possibile senza infrastruttura shadow.
- n piccolo -> il gate protegge dal disastro (rollback se chiaramente sotto), non
  certifica l'equivalenza fine.

Script di valutazione: `jobs/indici_gate.py` (sola lettura). Trigger automatico:
tappa nell'agenda (`jobs/sprint6_agenda.py`) che avvisa a >=25 trade indici chiusi.
