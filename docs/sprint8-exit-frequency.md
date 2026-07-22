# Sprint 8 — La frequenza del trailing (uscite) aiuta? (pre-registrato)

Data: 2026-07-22. Stato: **PRE-REGISTRATO prima di calcolare gli esiti.**
Soglie fissate ORA, non si spostano dopo i numeri (anti-HARKing).

## Origine

Segue il test frequenza-ingresso (`docs/sprint8-scan-frequency.md`, TIMING
IRRILEVANTE): la leva non e' l'ingresso ma la GESTIONE delle uscite. Idea utente:
allora rendiamo piu' frequente il controllo delle uscite. Questo lo verifica.

## Cosa e' testabile e cosa no

La gestione uscite ha due componenti (frequenze reali sulla VM):
- **Trailing stop** (`jobs/trailing_stop`, ogni 5 min): DETERMINISTICO, gratis,
  alza lo stop verso il profitto. **Backtestabile.** Oggetto di questo test.
- **Monitor LLM** (`jobs/monitor`, ogni 30 min): decide chiusure anticipate,
  COSTA token, NON riproducibile → NON backtestabile qui. Valutazione separata.

Fatto strutturale: lo stop-loss e' server-side su Capital (hit in tempo reale a
ogni istante). La frequenza del trailing NON cambia la protezione hard; cambia
solo quanto in fretta lo stop SEGUE il prezzo verso il profitto (cattura del picco
vs give-back tra un ciclo e l'altro).

## Metodo (dati orari, riuso motore live)

Stessi ENTRY del v1-momentum (ritardo D=1, come Fase 1 ingresso) per tutte le
condizioni: si isola SOLO l'effetto uscita. Variabile: **K = ogni quante barre
orarie il trailing AGGIORNA lo stop** (K ∈ {1, 2, 4}). L'HIT dello stop e del
target e' controllato a OGNI barra (server-side), ma lo stop applicato resta
"congelato" al valore dell'ultimo aggiornamento fino al tick K successivo; il
picco (peak_r) si aggiorna a ogni barra ma influenza lo stop solo al prossimo
aggiornamento. K=1 = trailing valutato ogni ora (fine); K=4 = ogni 4 ore (grosso).

Motore: SL k·ATR, TP rr·SL, trailing D+V1+V2 (OFFSET_FN), spread bid/ask reale,
overnight fee (riuso `jobs/backtest_run.py`). Metrica: expectancy_R netta.

Il live gira gia' a 5 min (piu' fine di 1h): questo test misura la SENSIBILITA'
alla frequenza nell'intervallo orario. Se gia' qui e' piatta, quasi certamente
5min→1min non aiuta. Se conta, serve Fase 2 con candle fini per vedere se il
guadagno persiste sotto l'ora o satura.

## Gate PRE-REGISTRATO

Sia `exp(K)` l'expectancy_R netta con aggiornamento trailing ogni K barre.

| Verdetto | Condizione | Azione |
|---|---|---|
| **FREQUENZA CONTA** | `exp(1) − exp(4) ≥ +0.10R` **e** monotòno (exp(1)≥exp(2)≥exp(4)) **e** robusto (senza i 2 migliori trade di K=1 il vantaggio resta ≥ +0.05R) | Aggiornare piu' spesso migliora la cattura → FASE 2 (candle fini: 5min vs 1min sotto l'ora). |
| **FREQUENZA IRRILEVANTE** | `|exp(1) − exp(4)| < 0.05R` | I 5 min attuali bastano gia'. Nessuna modifica, niente dati fini. |
| **INDECISO** | tutto il resto | Decide l'utente se passare a Fase 2. |

Nota: expectancy attesa negativa in assoluto (l'ingresso v1-momentum e' a edge<0,
Fase 1). Qui conta il DELTA tra le K, non il livello: si misura se la gestione piu'
fine recupera R, non se la strategia e' profittevole.

## ESEGUITO 2026-07-22 — FREQUENZA IRRILEVANTE (Fase 2 NON attivata)

Script `jobs/exit_frequency_backtest.py`, 6274 entry (stessi per tutti i K).

| trailing ogni | n | exp_R netta | win | mediana |
|---|---|---|---|---|
| K=1 (1h) | 6274 | -0.1031 | 34% | -0.184 |
| K=2 (2h) | 6274 | -0.1006 | 34% | -0.191 |
| K=4 (4h) | 6274 | **-0.0887** | 34% | -0.209 |

`exp(K=1) − exp(K=4) = -0.0144R`: aggiornare il trailing piu' spesso NON migliora,
anzi il segno (non significativo, sotto soglia) e' a favore del MENO frequente.
`|diff| < 0.05R` → **FREQUENZA IRRILEVANTE.** I 5 min live bastano gia'; scendere
sotto non aiuta. Fase 2 (candle fini) NON attivata.

**Lettura.** Il micro-segno pro-K4 e' coerente con la storia del trailing: un
trailing piu' reattivo stringe lo stop su rumore (whipsaw) e taglia i runner prima
che il movimento maturi (cfr. trailing troppo stretto, `docs/sprint4-trailing-*`).
Non e' un effetto da sfruttare (sotto soglia), ma esclude che "piu' frequente"
aiuti.

**Cosa NON copre:** solo il trailing DETERMINISTICO. Il monitor LLM (30 min) non e'
backtestabile (non riproducibile). Ma il ragionamento lo sconsiglia: costa il
doppio di token a 15 min, e su trade che durano ore/giorni cogliere un'inversione
15 min prima ha beneficio marginale piccolo; il timing d'uscita fine e' gia' piatto
qui. Nessuna prova che valga il costo.

**Azione: nessuna.** Trailing resta a 5 min, monitor a 30 min. Chiarimento chiave:
"la leva e' la gestione uscite" significa il COME (calibrazione del trailing:
`docs/sprint4-trailing-calibration.md`; decisioni del monitor: A1), NON il QUANTO
SPESSO. La frequenza non e' una leva, ne' in ingresso ne' in uscita.
