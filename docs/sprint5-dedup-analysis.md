# Sprint 5 — Dedup 24h: analisi cost/benefit (pre-registrata)

Data: 2026-06-26. Analisi offline, sola lettura. Decide se la finestra dedup
24h (oltre al fix direction-aware gia' deployato) va accorciata/sostituita o
tenuta. Pre-registrazione PRIMA di calcolare gli esiti.

## Contesto

Il dedup 24h blocca le ri-proposte di un asset gia' segnalato nelle 24h. Il fix
direction-aware (commit `3e1167e`) ha gia' corretto il blocco della direzione
opposta. Resta da capire se la **finestra 24h stessa** ha senso: blocca
prevalentemente ri-entrate che sarebbero state WINNER (trend che continuano →
dedup costa soldi) o LOSER (tesi gia' esaurite → dedup protegge)?

Caso scatenante: il 26/06 il top di 7 scan su 11 era Nikkei short 7.5, bloccato
dal dedup dopo la prima volta. Se Nikkei ha continuato a scendere erano winner
persi; se ha invertito, dedup ha protetto.

## Pre-registrazione (scritta PRIMA dei risultati)

**Campione:** ogni `scanner_run` con `outcome=no_setup` il cui TOP candidato
aveva score >= 7.0 (sarebbe stato apribile) E la cui (asset, direzione) era gia'
stata segnalata nelle 24h precedenti → "setup azionabile bloccato dal dedup".

**Simulazione dell'esito che NON c'e' stato:** entry al prezzo dell'asset
all'ora dello scan (close candela 4H); SL/TP = mediana di stop%/target% dei
signal reali; cammino in avanti su candele 5m (fallback 15m) fino a 48h, uscita
al primo tocco SL o TP, altrimenti close a 48h. R = esito in multipli di R.

**Metrica decisiva:** (a) % di setup bloccati che sarebbero stati WINNER, (b) R
medio simulato dei setup bloccati.

**Predizione (mia, prima di guardare):** i setup bloccati hanno R medio
**leggermente positivo** (~0 a +0.3R). Razionale: un momentum-follower che
riscora >=7.5 ripetutamente sullo stesso asset+direzione lo fa perche' il trend
e' reale e continua; quindi il dedup taglia piu' continuazioni-winner che
reversioni-loser. Ma l'effetto e' atteso modesto e regime-dipendente (nei giorni
choppy le ri-entrate sono loser). NON mi aspetto un R medio fortemente positivo.

**Criterio di decisione (pre-fissato):**
- R medio bloccati **> +0.2R** → il dedup 24h costa soldi: accorciare la finestra
  (es. 6-8h) o sostituirlo col solo tetto B1 (open-position) + cap per-direzione.
- R medio bloccati **< -0.2R** → il dedup protegge: tenere la finestra 24h.
- in mezzo (-0.2..+0.2) → neutro: tenere per sicurezza (anti thesis-stacking),
  il valore del dedup e' la protezione, non l'edge.

Niente HARKing: se emerge un taglio "ancora migliore" guardando gli esiti, si
pre-registra a parte, non si rivendica.

## Esiti (2026-06-26, `jobs/dedup_analysis.py`, finestra dal 12/06)

Mediana reale usata nella sim: stop 1.55%, target 3.2% (R:R 2.06).
**62 setup azionabili bloccati dal dedup** (score>=7, asset+dir gia' segnalata
nelle 24h); 49 con candele forward disponibili.

| asset | dir | n | R medio | win |
|-------|-----|---|---------|-----|
| Brent | short | 37 | **+1.12** | 28/37 |
| Nasdaq | long | 6 | −0.07 | 2/6 |
| Nasdaq | short | 5 | −0.52 | 2/5 |
| Bitcoin | long | 1 | +2.06 | 1/1 |
| **TOTALE** | | **49** | **+0.824** | **33/49 (67%)** |

**La predizione e' falsificata nel verso ma sottostimata nel magnitudo**: avevo
predetto +0 a +0.3R, l'esito e' **+0.82R**. Per il criterio pre-fissato
(R medio > +0.2 → il dedup costa) il verdetto e': **il dedup 24h sta costando
soldi** — blocca prevalentemente ri-entrate che sarebbero state winner.

### Lettura onesta (il caveat che ridimensiona)

- **Dominato da Brent short (37 su 49).** Il +0.82 e' quasi tutto "il dedup ha
  bloccato ri-entrate Brent short durante un downtrend Brent prolungato, e quelle
  continuavano a vincere". E' lo stesso pattern di concentrazione gia' visto
  (Brent domina ogni taglio). Non e' una verita' generale: e' regime-specifico
  (periodo di trend forte su Brent).
- **I casi Nasdaq (11/49) sono negativi** (R −0.07 e −0.52): li' il dedup ha
  PROTETTO (ri-entrate su tesi che si invertivano). Esattamente la sua funzione.
- Quindi il dedup **costa quando l'asset trenda** (vuoi ri-entrare) e **protegge
  quando e' choppy**. La media e' positiva perche' il sample e' Brent-trend-heavy.

### Il punto che il numero NON cattura (rischio)

Il +0.82 assume che avresti VOLUTO aprire tutte le 37 ri-entrate Brent short. Ma
sarebbero ~37 short Brent in 3 settimane: **concentrazione estrema su una sola
tesi**. Anche vincendo +1.12R l'una, e' un libro indifferenziato; se Brent avesse
invertito, perdita correlata pesante. Quindi "il dedup costa +0.82R" e' vero
sull'edge ma ignora che rimuoverlo del tutto aprirebbe il thesis-stacking che il
dedup esiste per impedire.

## Decisione

Il criterio (R medio +0.82 > +0.2) dice **il dedup 24h e' troppo restrittivo**.
Ma la conclusione NON e' "rimuoverlo" (aprirebbe lo stacking pericoloso): e'
**sostituire la finestra cieca 24h con controlli a cap**, che gia' abbiamo:
- il fix **direction-aware** (gia' deployato) gia' recupera meta' del problema
  (le direzioni opposte);
- per la STESSA direzione: lasciar ri-entrare DOPO una chiusura (il tetto B1 gia'
  blocca i duplicati mentre la posizione e' aperta), **ma con un cap di
  ri-entrate per-asset-per-giorno** (es. max 2) per bloccare lo stacking. Cosi'
  catturi la continuazione senza aprire 37 Brent short.

Alternativa minima: accorciare la finestra da 24h a ~6-8h (cattura le
continuazioni infragiornaliere, blocca lo stacking multi-giorno).

**Da decidere con l'utente, NON auto-deployato**: e' un cambio che aumenta
frequenza e concentrazione su denaro reale, va validato come gli altri.

## Limiti

Sample Brent-dominato (regime-specifico, non generalizzabile). SL/TP = mediana
reale, non quelli effettivi del setup bloccato. Entry al close candela, non al
tick. Sim senza spread/slippage. Forward 48h. Direzioni, non significativita'.


## Limiti

SL/TP simulati con la mediana reale, non con lo stop/target effettivo del setup
bloccato (non loggato in scanner_runs). Entry al close 4H, non al tick esatto
dello scan. Sim senza spread/slippage. Forward 48h arbitrario. Sample limitato
alle ~3 settimane di scanner_runs disponibili. Direzioni, non significativita'.
