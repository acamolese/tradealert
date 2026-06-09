# Sprint 4 troncone 2 (rivisto) — chasing: controllo ATR e setup validazione

Data: 2026-06-09. Esegue la Task 1 (metrica ATR retrospettiva, gate) richiesta
dal consulente, e predispone Task 2/3 in stato condizionato all'esito del gate.
Sola lettura del DB (anon key, le select passano). Script riproducibile:
`jobs/chasing_analysis.py`. Nessuna modifica al sistema, monitor invariato.

**Sintesi in una riga**: la metrica ATR *come specificata nel doc* (distanza in
ATR dal livello di trigger/swing) **non conferma** il proxy daily: correlazione
con l'esito praticamente nulla. Un segnale residuo sopravvive solo nella forma
daily (grezza o normalizzata per ATR), ma è debole e ha già un controesempio nel
trade più recente. Il gate pre-registrato scatta verso "fragile": logging e
pre-registrazione restano **in attesa di decisione**, non attivati.

## Task 1 — Metrica ATR-dal-trigger, retrospettiva

### Cosa ho costruito

Tre metriche, tutte firmate per la direzione del trade (alto = entro dopo un
movimento già ampio nella mia direzione, cioè "inseguo"):

- `chasing_daily` = `daily_pct_change` firmato. Il proxy del doc precedente.
- `chasing_atr_swing` = distanza in ATR dal livello di swing, firmata:
  long `(last − low_20)/atr_4h`, short `(high_20 − last)/atr_4h`. **Questa è la
  metrica "distanza dal trigger del setup in ATR" indicata nel doc.** `low_20`/
  `high_20` sono gli estremi delle ultime 20 candele 4H (~3,3 giorni).
- `chasing_atr_daily` = `daily_pct_change` firmato / `atr_pct_of_price`. La
  traduzione ATR *del movimento giornaliero* (lo stesso proxy daily, ma
  normalizzato per la volatilità invece che per il prezzo). Riportata per
  confronto.

Dati da `signals.features_at_decision` (reali in DB: `last_price`, `atr_4h`,
`high_20`, `low_20`, `daily_pct_change`, `atr_pct_of_price`). `peak_R`: dal doc
`sprint3.5-entry-diagnosis` per 38-58 (candele 5m/15m); dall'evento nativo
`intra_trade_extreme` per 59-61. Perimetro: 24 trade chiusi (38-61), inclusi 3
post-58 con dati nativi.

### Tabella ricalcolata (ordinata per chasing_atr_swing decrescente)

`TP` = uscita `tp_hit` (TP pieno). pnl in €, peak_R proxy.

| id | asset | dir | pnl | peak_R | TP | ch_daily | ch_atr_swing | ch_atr_daily |
|----|-------|-----|-----|--------|----|----------|--------------|--------------|
| 46 | Brent | short | -2.52 | 0.45 |  | 3.38 | **5.40** | 1.86 |
| 53 | Gold | long | -1.59 | 0.08 |  | 1.16 | **4.33** | 1.52 |
| 45 | Nasdaq | long | 0.00 | 1.27 |  | 0.28 | **3.98** | 0.57 |
| 59 | Gold | short | **1.83** | 1.60 |  | 2.37 | **3.90** | 2.60 |
| 56 | Brent | long | -3.31 | 0.05 |  | 2.07 | 3.68 | 1.25 |
| 52 | Bitcoin | short | **2.01** | 2.08 | Y | 1.92 | 3.52 | 2.73 |
| 40 | Nasdaq | long | **4.16** | 1.12 |  | 0.40 | 3.48 | 0.55 |
| 58 | Nasdaq | short | **6.77** | 2.10 | Y | 0.88 | 3.36 | 1.34 |
| 47 | Gold | short | -4.00 | -0.09 |  | 0.67 | 3.17 | 0.68 |
| 50 | Brent | short | -2.98 | 0.11 |  | 0.80 | 3.01 | 0.46 |
| 38 | Brent | short | -0.88 | 0.28 |  | 5.05 | 2.84 | 2.59 |
| 55 | Brent | long | -0.06 | 1.09 |  | 0.68 | 2.82 | 0.39 |
| 49 | Brent | short | **9.46** | 2.17 | Y | 0.02 | 2.67 | 0.01 |
| 48 | Nasdaq | long | **2.82** | 1.41 |  | 0.41 | 2.43 | 0.63 |
| 60 | Nasdaq | long | -1.90 | 0.16 |  | 2.40 | 2.36 | 2.44 |
| 54 | Nasdaq | long | -1.10 | 0.77 |  | 0.25 | 2.30 | 0.50 |
| 42 | Brent | short | **10.85** | 2.93 | Y | -0.81 | 2.25 | -0.38 |
| 43 | Gold | long | -1.93 | 0.52 |  | 1.25 | 2.06 | 2.09 |
| 39 | Nasdaq | long | -0.79 | 0.38 |  | 1.44 | 1.96 | 1.63 |
| 51 | Brent | long | -0.11 | 1.16 |  | 2.39 | 1.85 | 1.46 |
| 41 | Gold | long | -0.19 | 0.13 |  | -0.39 | 1.75 | -0.50 |
| 44 | Brent | long | -2.44 | 0.68 |  | 2.69 | 1.19 | 1.23 |
| 61 | Brent | long | -0.46 | 0.46 |  | 1.55 | 0.79 | 0.92 |
| 57 | Gold | long | -1.61 | 0.90 |  | 0.73 | 0.65 | 1.06 |

Si legge a occhio: ordinando per `chasing_atr_swing`, i WIN (#59, #52, #40, #58,
#48, #49, #42) sono **sparsi su tutta la colonna**, dal rank 4 al rank 17.
Nessuna struttura.

### Controllo di robustezza (quantitativo, n=24)

Correlazioni (Pearson / Spearman) con l'esito:

| metrica | vs pnl | vs peak_R |
|---------|--------|-----------|
| chasing_daily | **−0.44 / −0.44** | −0.35 / −0.29 |
| chasing_atr_swing | **+0.01 / +0.02** | +0.03 / −0.02 |
| chasing_atr_daily | −0.34 / −0.24 | −0.17 / −0.12 |

Concordanza tra metriche: `chasing_daily` vs `chasing_atr_swing` = **0.18**
(quasi ortogonali); `chasing_daily` vs `chasing_atr_daily` = 0.80 (è lo stesso
movimento riscalato).

Separazione media (alto = inseguo):

| metrica | full TP | resto | WIN | LOSS |
|---------|---------|-------|-----|------|
| chasing_daily | 0.50 | 1.48 | 0.74 | 1.55 |
| chasing_atr_swing | 2.95 | 2.70 | 3.09 | 2.60 |
| chasing_atr_daily | 0.92 | 1.20 | 1.07 | 1.19 |

Rank dei TP grandi (1 = più inseguitore su 24):

| metrica | #42 (+10.85) | #49 (+9.46) | full TP ai rank |
|---------|--------------|-------------|------------------|
| chasing_daily | 24 (fondo) | 22 | 24, 22, 13, 8 |
| chasing_atr_swing | 17 (mezzo) | 13 | 17, 13, 8, 6 |
| chasing_atr_daily | 23 | 22 | 23, 22, 10, 1 |

### Risposte alle tre domande del task

1. **La separazione regge anche con la metrica ATR?** No, non con la metrica
   ATR del doc (`chasing_atr_swing`): correlazione con pnl `+0.01`, con peak_R
   `+0.03`, e separazione WIN/LOSS **invertita** (WIN 3.09 > LOSS 2.60). È
   rumore. Regge solo, e indebolita, con `chasing_atr_daily` (pnl `−0.34`).
2. **I 2 TP grandi restano in fondo?** Con `chasing_daily` sì (rank 24 e 22).
   Con `chasing_atr_swing` **no**: scivolano a metà classifica (17 e 13). Con
   `chasing_atr_daily` sì (23 e 22).
3. **La cima resta tutta loss?** Con `chasing_daily` sì (top5 = 5/5 loss). Con
   le versioni ATR no: in cima compaiono WIN a bassa volatilità (#59 +1.83, #52
   +2.01, #58 +6.77), perché dividere per un ATR piccolo gonfia il loro
   inseguimento apparente.

### Verdetto del gate

La metrica ATR *come richiesta* (distanza dal trigger in ATR) **non conferma il
proxy daily**: porta il segnale a zero. Per il gate pre-registrato
("se la versione ATR NON conferma, il segnale era fragile, ci fermiamo a costo
zero") questo è il ramo negativo.

Tre precisazioni oneste, perché il quadro non è del tutto nero:

- **Non è solo volatilità.** La volatilità da sola correla `+0.08` con il pnl
  (nulla, coerente col troncone 2). Quindi `chasing_daily` (−0.44) non è la
  volatilità travestita: il segno e l'ampiezza del movimento giornaliero
  *firmato per direzione* portano informazione che la volatilità grezza non ha.
- **Un residuo sopravvive nella forma daily-ATR** (−0.34): la relazione "più il
  prezzo si è già mosso nella mia direzione, peggiore l'entry" regge alla
  normalizzazione per volatilità, ma la separazione categorica (TP pieni, WIN/
  LOSS) quasi sparisce. È un gradiente continuo debole, non una soglia netta.
- **Il dato più recente la contraddice.** #59 (Gold short, post-58, il più
  vicino al forward) ha `chasing_daily` 2.37 ed è un WIN +1.83 con peak 1.60: un
  inseguitore conclamato che ha funzionato. Il primo punto quasi-forward è già
  un controesempio.

Lettura complessiva: la pulizia del proxy daily era in parte un artefatto della
metrica grezza e del campione (terza metrica sullo stesso sample 38-58, come
notato). Il segnale è reale ma **debole e sensibile a come si costruisce la
metrica**; la versione operativamente più sensata (ancorata al trigger in ATR)
non lo regge.

## Task 2 — Logging in produzione: NON attivato (gate)

Patch progettata ma **non applicata**, in attesa di decisione sul gate. Sarebbe
in `src/scanner.py` alla riga ~1710, dove `asset_features` e `top.direction`
sono già disponibili prima di `insert_signal` (riga 1730). Forma additiva,
try/except, zero impatto su scoring/aperture, indipendente dal flag
`SCORING_TWO_CALL` del troncone 1:

```python
# dopo: asset_features = features.get(top.asset, {})
try:
    _add_chasing_features(asset_features, top.direction)  # daily + atr_swing + atr_daily
except Exception:
    log.debug("chasing features non calcolate", exc_info=True)
```

con un helper in `src/features.py` che replica le tre formule sopra. Se la
decisione è procedere, lo applico e committo in un passaggio isolato.

Motivo del non-aver-proceduto: il gate è una decisione pre-registrata vostra, e
il suo ramo "ATR non conferma" è scattato sulla metrica richiesta. Modificare il
codice live (anche additivo) prima della vostra conferma contraddirebbe il gate.

## Task 3 — Pre-registrazione: proposta condizionata, con onestà sulla potenza

Premessa: `chasing_atr_swing` è da scartare come base (segnale nullo). Una
pre-registrazione difendibile potrebbe poggiare solo su `chasing_daily` (l'unica
con segnale apprezzabile). Sensibilità di `chasing_daily` sul sample (per
calibrare X):

| soglia X | n trade > X | % peak_R < 0.5 | full TP sopra X | WIN sopra X |
|----------|-------------|----------------|------------------|-------------|
| 1.5 | 9 | 56% | 1 (#52) | 2 (#59, #52) |
| 2.0 | 7 | 57% | 0 | 1 (#59) |
| 2.5 | 3 | 67% | 0 | 0 |
| 3.0 | 2 | 100% (n=2) | 0 | 0 |

Lettura della sensibilità: la predizione "nessun TP pieno sopra X" regge solo da
X ≥ 2.0 (i TP pieni arrivano a `chasing_daily` 1.92, #52), con margine di 2-3
trade: fragile. La predizione "peak_R < 0.5 in ≥ Y% dei casi" è **debole**: a
X=2.0 si ferma al 57%, appena sopra il 50% del caso, perché #59 (1.60) e #51
(1.16) sono inseguitori che si sono sviluppati. Solo spingendo X a 2.5-3.0 la
quota sale, ma con n=2-3 (non testabile in avanti su 10-15 trade).

Bozza pre-registrazione (da fissare voi, oppure da **non** fissare se il gate
chiude):

- Primaria (debole): *"Sui prossimi 15 trade aperti, nessun trade con uscita TP
  pieno avrà `chasing_daily` > 2.0."* Difendibile sul sample ma a basso potere:
  i TP pieni sono rari (~17% base rate), su 15 trade se ne attendono ~2-3, quindi
  la predizione discrimina poco.
- Secondaria (informativa, non come prova): *"tra i trade con `chasing_daily` >
  2.0, peak_R < 0.5 in ≥ 55% dei casi."* Soglia onesta vicino al caso; serve a
  raccogliere, non a dimostrare.

Non propongo X sulla metrica ATR: lì non c'è niente da pre-registrare.

## Decisione (2026-06-09): STOP a costo zero

Andrea ha scelto l'opzione 1: **stop**. La pista chasing si archivia come
**terzo risultato negativo** dopo lo score (Sim C) e la volatilità (troncone 2):
nessuno dei tre denoising/feature testati sul sample 38-58 produce un segnale
robusto e operativizzabile sulla qualità entry. Nessuna modifica al codice,
nessun logging, nessuna pre-registrazione. Resta `jobs/chasing_analysis.py` per
ripetere l'analisi quando il campione sarà più ampio (e con `peak_R` nativo
anche sui trade più vecchi, non disponibile ora). Il lavoro sull'entry, se
ripreso, parte da un segnale **diverso** da score / volatilità / estensione del
movimento (le tre dimensioni ora escluse).

## Raccomandazione (registrata sopra, non più aperta)

Il gate, applicato alla lettera (metrica ATR del doc → segnale nullo), dice
**fermarsi**. Il residuo daily è reale ma troppo debole per fondare una
pre-registrazione di potere adeguato, e il primo dato quasi-forward (#59) già lo
contraddice. Le tre opzioni valutate al momento della decisione:

1. **Stop a costo zero** (gate stretto): la pista chasing non regge alla prova
   ATR, si archivia come terzo risultato negativo dopo score e volatilità. Resta
   `jobs/chasing_analysis.py` per ripetere l'analisi a campione più ampio.
2. **Solo logging, senza pre-registrazione forte**: attivo il logging additivo
   (Task 2) per accumulare `chasing_daily`/`chasing_atr` nativi sui trade
   futuri, ma rinuncio a una predizione pre-registrata (non ce n'è una di potere
   sufficiente). Si rivaluta tra 20-30 trade con dati nativi e campione fresco.
3. **Logging + pre-registrazione debole** (bozza Task 3): si accetta una
   predizione a basso potere, sapendo che 10-15 trade difficilmente la
   falsificheranno in modo netto.

La mia preferenza: opzione 2. Il segnale non è morto (daily −0.44, non-
volatilità), ma è troppo costruzione-sensibile per impegnarsi su una soglia ora;
il logging nativo è l'unico modo per riprovare in avanti senza ri-minare un
campione esaurito, e non costa nulla. L'intervento (penalità deterministica vs
feature LLM) resta comunque rinviato a dopo la validazione forward, come da
vincolo.

## Limiti

n=24 (Brent 11, gli altri pochi per asset): direzioni, non significatività.
`peak_R` 38-58 è proxy da candele; 59-61 nativo ma 3 trade. `chasing_atr_swing`
usa `low_20`/`high_20` a 4H (~3,3 giorni), scala diversa dal movimento
giornaliero: la sua ortogonalità col daily è in parte una differenza di
orizzonte, non solo di costruzione. Le correlazioni su 24 punti hanno intervalli
di confidenza ampi.
