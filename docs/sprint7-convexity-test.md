# Sprint 7 — Test di convessità sulle uscite (no-TP + time-stop)

Data: 2026-07-02. Stato: PRE-REGISTRATO prima dell'esecuzione.

## Ipotesi e perché è legittima (non è il "round 3" vietato)

Tutto il P&L realizzato vive in 5 runner (+44.5 EUR) contro 81 trade che
sommano -39. Il TP attuale taglia i winner a ~2R (i 7 TP-hit valgono +52.9).
L'uscita a euro fissi è già stata bocciata PROPRIO perché tagliava i winner.
Ipotesi: massimizzare la convessità — nessun TP (trailing-only) e kill
veloce dei trade che non partono. NON tocca le entrate (usa le 93 entrate
REALI del sistema): niente ricerca di segnali, solo ridisegno dell'uscita,
il lato dove l'evidenza di valore esiste (trailing calibrato, monitor A1).
Dichiarato: è il 4° test dell'harness; un esito positivo è PROVVISORIO e
va validato forward dietro flag, non deployato a fiducia.

## Protocollo (un run, tre varianti fisse, candele HOUR locali)

Per ognuno dei trade chiusi con signal collegato: entrata, direzione, R e TP
reali; simulazione dall'apertura con trailing live (D+V1+V2), orizzonte
15 giorni (mark-to-market se ancora aperto):

- **V0 (baseline)**: bracket attuale, TP al livello reale del trade.
- **V1 (no-TP)**: nessun TP, esce solo il trailing (o l'orizzonte).
- **V2 (no-TP + time-stop)**: come V1, più kill a chiusura barra se dopo
  72h il picco non ha mai toccato +0.5R.

Metriche: expectancy e R totale per variante sullo stesso stream di trade.
Confronto pulito sim-vs-sim (stesse candele, stesse assunzioni SL-first).

## Criterio di lettura (pre-registrato)

- **CONVESSITÀ SUPPORTATA** se V1 o V2 batte V0 di ≥ +0.15R di expectancy,
  E il vantaggio resta ≥ +0.10R togliendo i 2 migliori trade della variante
  vincente (anti #42/#49), E la mediana della variante non peggiora di più
  di 0.10R (il no-TP non deve pagare il runner con un'emorragia di mediana).
- Esito positivo → deploy GATED: flag `EXIT_NO_TP` in shadow/forward con
  gate a 20 trade, mai switch diretto.
- Esito negativo → si archivia: il TP resta.

## ESEGUITO 2026-07-02 — VERDETTO: NEGATIVO, il TP resta

Script `jobs/convexity_test.py`, 82 trade reali simulati (4 esclusi):

| Variante | exp | mediana | tot | senza top2 |
|---|---|---|---|---|
| V0 bracket con TP (baseline) | **+0.043R** | -0.085 | +3.55R | -0.013 |
| V1 no-TP trailing-only | +0.008R | -0.085 | +0.64R | -0.067 |
| V2 no-TP + time-stop 72h/0.5R | +0.011R | -0.080 | +0.92R | -0.064 |

Il no-TP libera davvero i runner (#42 Brent +0.90→+2.75R, #52 Bitcoin
+2.00→+3.25R), ma su questo stream di entrate i mid-winner che oggi
chiudono a TP ~2R restituiscono al trailing più di quanto i runner
guadagnino: delta expectancy -0.036R, e senza i top2 il no-TP perde
-0.11R vs baseline. Il time-stop scatta 3 volte su 82 e non sposta nulla.
Archiviato: **TP e time-stop non sono la leva**. Coerente col quadro: le
uscite attuali (TP + trailing + monitor) sono già vicine all'ottimo
raggiungibile; il limite del sistema è a monte, nel flusso di entrate
e nei costi, non nella gestione.
