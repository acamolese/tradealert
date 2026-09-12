# Sprint 5 — Rischio gap di weekend/sessione su HK50 e J225 (pre-registrato)

Data: 2026-06-30. Analisi offline, sola lettura. Decide se gli asset asiatici
del paniere trending (HK50 = Hang Seng, J225 = Nikkei) hanno un rischio
strutturale di slippage da gap che giustifica un fix, oppure se il caso #140
(trade #89) è un outlier. Pre-registrazione scritta PRIMA di calcolare gli esiti.

## Caso scatenante

Trade #89 (Hang Seng short, signal #140): chiuso `reconcile:stop_hit` lunedì
29/06 03:15 dopo essere stato aperto venerdì 26/06 18:06. Perdita reale **−7.83€**
contro uno stop teorico di ~**−3.9€** (stop 1.4%, size 0.1): ~**2× lo stop**, cioè
~**−2R** invece di −1R. La traiettoria intraday a 5m mostrava un calore massimo di
solo −0.55R: il danno è avvenuto in un buco che le candele 5m non vedono → HK50 ha
aperto lunedì oltre lo stop, saltandolo. Sospetto: rischio strutturale degli asset
che gappano (chiusura sessione asiatica ogni notte + weekend), non caso isolato.

## FASE 1 — Misura (questo documento)

Due parti complementari, perché la storia-trade è povera (HK50: 1 solo stop_hit;
J225: zero trade) e da sola non basta a stabilire "sistematico".

### Parte A — Struttura di mercato (n-independent, spina dorsale)

Dalle candele HOUR Capital degli ultimi ~40 giorni, per ciascun asset
(HK50, J225, Brent, Gold, Bitcoin) si misura la distribuzione dei **gap tra
candele consecutive** separate da un buco temporale (chiusura sessione/weekend):
- `gap%` = |open_dopo − close_prima| / close_prima · 100 (prezzi mid);
- classificazione del buco: **weekend** se l'intervallo attraversa sabato/domenica
  (o dt > 24h), **daily/sessione** se 2h ≤ dt ≤ 24h, ignorato se intraday;
- conversione in **R-equivalente** = `gap% / stop%_mediano` dell'asset (stop%
  mediano dai signal storici; per J225, senza signal, si usa lo stop% mediano di
  HK50 come proxy index asiatico, dichiarato).

Aggregati per asset e per tipo (weekend / daily): n, mediana, 90° percentile
(`p90`), max di `gap%` e di R-equivalente.

### Parte B — Slippage realmente subito (trade-based)

Su tutti i trade chiusi `reconcile:stop_hit` di HK50, Brent, Gold, Bitcoin
(J225 N/A: nessun trade): R realizzato `R_real = pnl / R_eur` (con
`R_eur = entry·stop%/100·size·conv`, conv quote→EUR per-asset stimato dai trade),
**slippage oltre lo stop** = max(0, −R_real − 1) in R e in €, e flag
`held_through_gap` (l'intervallo opened→closed attraversa un weekend o una
chiusura sessione). Distingue gli stop colpiti in gap da quelli intraday.

## Domanda pre-registrata

Gli asset asiatici (HK50/J225) hanno uno slippage-da-gap **sistematicamente
peggiore** degli asset continui (Brent/Bitcoin)? E quanto del rischio è
**concentrato sulle posizioni tenute attraverso il weekend** rispetto ai gap
giornalieri di sessione?

**Predizione (mia, prima di guardare):** sì, HK50/J225 hanno gap **giornalieri**
(chiusura sessione asiatica → riapertura) oltre a quelli di weekend, mentre
Bitcoin è 24/7 (gap ~0) e Brent è quasi-continuo (gap piccoli, solo weekend).
Mi aspetto weekend `p90` R-equiv per HK50/J225 nell'ordine di **0.5–1.5R** e gap
daily non trascurabili (~0.3–0.6R), contro <0.2R per Brent e ~0 per Bitcoin. Il
rischio è in buona parte sui weekend ma NON solo: anche l'overnight asiatico
gappa, quindi "non aprire il venerdì" da solo potrebbe non bastare.

## FASE 2 — Fix (SOLO se la Fase 1 conferma; da leggere insieme, niente auto-deploy)

Tre opzioni:
- **(a)** Non aprire posizioni HK50/J225 a ridosso della chiusura settimanale
  (venerdì dopo ~13:00 UTC) → non si tengono attraverso il gap di weekend. La più
  semplice, basso impatto, ma non protegge le posizioni già aperte né i gap daily.
- **(b)** Margine extra sullo stop per gli asset gap-prone (stop più largo che
  assorbe il gap atteso). Cambia sizing e R → invasiva, da toccare con cautela.
- **(c)** Chiusura forzata delle posizioni HK50/J225 prima della chiusura
  weekend. Protegge anche le posizioni già aperte.

### Soglie di decisione (pre-fissate)

Definizioni: `WG_p90` = 90° pct del gap **weekend** in R-equiv; `DG_p90` =
90° pct del gap **daily/sessione** in R-equiv; il confronto è col max(Brent,
Bitcoin) sullo stesso metro.

1. **Rischio NON confermato** → archiviare, #140 = outlier, non toccare nulla, se:
   HK50/J225 `WG_p90` < 0.5R **oppure** non materialmente peggiore dei continui
   (< 2× max(Brent, Bitcoin)).
2. **Rischio confermato e solo-weekend** (`WG_p90` ≥ 0.5R e ≥ 2× continui, MA
   `DG_p90` < 0.3R) → **opzione (a)**: blocco nuove aperture HK50/J225 venerdì
   dopo le 13:00 UTC.
3. **Confermato con gap weekend grandi** (`WG_p90` > 1.0R): le posizioni già
   aperte possono essere saltate di oltre 1R → **opzione (c)** (chiusura forzata
   pre-weekend) in aggiunta o al posto di (a), perché bloccare solo le nuove
   aperture lascia esposte quelle in essere.
4. **Confermato e ANCHE daily** (`DG_p90` ≥ 0.5R, ogni overnight asiatico gappa
   oltre mezzo stop) → **opzione (b)** (margine strutturale sullo stop per questi
   asset), perché evitare i weekend non basta. Scelta solo in questo caso, data
   l'invasività.

Niente HARKing: se emergesse un taglio "migliore" guardando gli esiti, si
pre-registra a parte. Le soglie sopra non si spostano dopo aver visto i numeri.

## Esiti (2026-06-30, `jobs/weekend_gap_analysis.py`, 40gg HOUR)

### Parte A — Struttura di mercato (gap in R-equivalente)

| asset | daily R_p90 | **weekend R_p90** | weekend gap%_p90 | stop% |
|-------|-------------|-------------------|------------------|-------|
| Hang Seng | 0.02 | **0.50** | 0.72% | 1.45 |
| Nikkei | 0.07 | **0.98** | 1.43% | 1.45 (proxy) |
| **Brent Oil** | 0.27 | **1.50** | 3.76% | 2.5 |
| Gold | 0.07 | 0.34 | 0.47% | 1.4 |
| Bitcoin | 0.00 | 0.32 | 1.13% | 3.5 |

Sintesi weekend R_p90 vs max(continui)=Brent 1.50: **Nikkei 0.7×, Hang Seng 0.3×**.

### Parte B — Slippage reale (trade reconcile:stop_hit)

16 stop_hit storici. Slippage oltre lo stop ~0 su quasi tutti (il sistema riempie
allo stop con precisione, R_real ≈ −1.0). Due eccezioni:
- **#89 Hang Seng −7.83€: R_real −1.99, slippage +0.99R (+3.89€)**, held-weekend.
  Unico caso grave dell'intera storia.
- #88 Brent −5.65€: R_real −1.11, slippage +0.11R. Minimo.

Slippage medio per asset: Brent +0.01R (n=8, held-weekend 0), Gold +0.01R (n=7,
held-weekend 2 con slip ~0, uno addirittura in profitto #59), **Hang Seng +0.99R
(n=1)**.

### Lettura — la predizione è FALSIFICATA

1. **Gli asset asiatici NON sono peggio dei continui.** È **Brent** (che avevo usato
   come baseline "continuo sicuro") ad avere i gap di weekend più grandi in R
   (p90 1.50R, max 3.76%). HK50 0.50R e J225 0.98R stanno **sotto** Brent.
2. **I gap daily/overnight di HK50/J225 sono trascurabili** (0.02–0.07R): la mia
   paura "anche l'overnight asiatico gappa" è infondata. L'opzione (b) (margine
   strutturale per gap daily) non ha alcuna base.
3. **#89 è un outlier n=1, non un pattern.** Il suo slip 0.99R è più grande del
   massimo weekend gap HK50 osservato (0.72% = 0.50R): non è il "gap tipico"
   dell'asset. La causa è il **timing d'ingresso**: aperto venerdì 26/06 18:06 UTC,
   cioè **fuori dalla sessione cash di HK** (sessione asiatica ~01:30–08:00 UTC),
   su quotazione sottile, e tenuto attraverso il weekend → lunedì ha combinato
   deriva avversa + un gap di coda. Non è "HK50 gappa sempre".

## Raccomandazione

**Criterio pre-fissato #1 (rischio NON confermato → archiviare):** HK50/J225 non
sono materialmente peggiori dei continui (ratio 0.3×/0.7×, ben sotto il 2×
richiesto), e i gap daily sono nulli. **Verdetto: archiviare. #140/#89 è un
outlier, nessun fix HK50/J225-specifico è giustificato. Niente in produzione.**

Le opzioni (a)/(b)/(c) NON si attivano: erano subordinate alla conferma, che non
c'è.

**Finding collaterale (NUOVO, non azionato qui):** il rischio gap-di-weekend
esiste ma è **generale e trasversale**, non asiatico, ed è massimo su **Brent**
(p90 1.50R). Se si vuole affrontare il rischio di tenere posizioni a stop stretto
attraverso il weekend, va fatto come regola **generale** (tutti gli asset, Brent in
testa) e **pre-registrato a parte** con il suo gate, non come patch sugli asiatici.
Possibile spunto a basso impatto: evitare aperture in sessioni sottili/fuori-orario
poco prima di una chiusura prolungata (è ciò che ha davvero fatto male a #89), ma
è un'ipotesi da validare separatamente, non una conclusione di questa analisi.

## Limiti

Candele HOUR (~40gg, limite 1000/asset): i gap intra-giornata più fini non sono
visti, ma quelli di sessione/weekend sì. R-equiv usa lo stop% mediano, non quello
del singolo trade. J225 stop% = proxy HK50. Parte B con n piccolo (HK50 n=1) →
indicativa, non significativa; il peso lo porta la Parte A. Gap misurati sui mid,
senza spread. Direzioni e ordini di grandezza, non test statistici.
