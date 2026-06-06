# Sprint 4 troncone 1 — Shadow test scoring temp 0.2

Data avvio: 2026-06-06. Stato: **shadow attivo in produzione, in
accumulo**. Deploy della pipeline a due chiamate **NON ancora fatto**:
gated su questi dati. Monitor invariato.

## Cosa è stato deployato (additivo, non cambia i trade)

Commit `7ecf60c`. Ad ogni scan, se `SCORING_SHADOW=true` (attivo sulla VM),
lo scanner ri-scora lo STESSO universo a **temp 0.2** subito dopo il
ranking reale (temp 1.0) e logga entrambi su `monitoring_events`
(`event_type='scoring_shadow'`), senza agire sullo score shadow. La
decisione di apertura resta governata solo dallo score reale temp 1.0.

- `rank_setups(..., temperature=None)`: default invariato (omessa → 1.0).
- Flag `SCORING_SHADOW` (default false): attivabile/disattivabile senza
  redeploy. Wrappato in try/except: un errore shadow non tocca lo scan.
- **Costo**: finché attivo lo shadow raddoppia il costo scanner (una
  `rank_setups` extra per scan, ~+$0.46/giorno). È il costo temporaneo
  della validazione: da spegnere appena c'è il verdetto.

## La domanda che lo shadow deve rispondere

Non "lo score è più stabile" (lo sappiamo dalla simulazione), ma il punto
critico: **temp 0.2 cambia QUALI candidati superano la soglia 7 rispetto a
temp 1.0?** Se apre lo stesso set di trade (solo più stabile), si deploya.
Se apre un set diverso (es. sistematicamente meno trade), è un cambio di
comportamento da capire prima.

## Segnale preliminare (dalle simulazioni, da confermare in shadow)

Indizio di rischio dalle Sim A/C: a temp 0.2 le medie sono leggermente
**più basse** su alcuni asset (Gold 7.23→7.12, Bitcoin 4.87→4.50; Nasdaq
invariato). Combinato col Finding 2b (il **43% degli open storici sta
esatto a 7.0**, sul filo), anche un piccolo abbassamento sistematico
potrebbe far scendere alcuni 7.0 sotto soglia → **aprire meno trade**.

Quindi l'ipotesi da verificare non è neutra: c'è una plausibile deriva
verso uno scoring più conservativo. Lo shadow dirà se e quanto.

## Criterio di valutazione (da eseguire sui dati accumulati)

Dopo qualche giorno (target: ≥ ~50 scan con almeno una manciata di scan
"produttivi"), per ogni evento `scoring_shadow`:
1. Top proposal reale (temp 1.0): score ≥ 7? (avrebbe aperto)
2. Top proposal shadow (temp 0.2): score ≥ 7? sullo stesso asset?
3. **Tasso di accordo** sulla decisione apri/non-apri. I disaccordi sono i
   cambi di comportamento da capire.
4. Per gli asset borderline (score reale 7.0-7.5): lo shadow li tiene sopra
   soglia o li fa scendere? Direzione e ampiezza dello shift.

Query (eseguibile a mano):
`select details from monitoring_events where event_type='scoring_shadow'
order by created_at` → confrontare `real[0].score≥7` vs `shadow` sullo
stesso asset.

### Soglia di decisione
- **Deploy**: se lo shadow apre sostanzialmente lo stesso set (accordo alto,
  nessuno shift sistematico che sposta molti borderline) → la temp bassa è
  un cheap win sano, si procede con la pipeline a due chiamate.
- **No-deploy / capire prima**: se temp 0.2 apre un set diverso in modo
  sistematico (es. molti meno trade) → va capito perché prima di cambiare,
  eventualmente ricalibrando la soglia (che però è esplicitamente fuori da
  questo troncone).

## Raccomandazione attuale: NO-DEPLOY, attendere i dati

La pipeline a due chiamate non si deploya finché lo shadow non conferma che
temp 0.2 non altera quali trade si aprono (o che l'alterazione è compresa e
accettata). Shadow avviato oggi; rivalutare dopo qualche giorno di scan.
Ricordarsi di **spegnere `SCORING_SHADOW`** una volta deciso (costo doppio).

## Limiti

Lo shadow gira solo nella finestra scan attiva (05-20 UTC). Il set di asset
è piccolo (5), quindi i "produttivi" (score ≥ 7) sono pochi per giorno: per
un campione decente di casi borderline servono più giorni. Tutto su dati di
produzione reali (a differenza delle simulazioni con universi sintetici),
quindi più affidabile, ma lento ad accumulare.
