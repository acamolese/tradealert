# Sprint 4 — Ipotesi tempismo dell'entry (nota per consulente)

Data: 2026-06-09. **Natura del documento**: nota di sintesi e ipotesi, non
un'analisi conclusiva con nuove query. Rilegge dati gia' in DB e gia' tabulati
in tre analisi precedenti, sotto una lente nuova (il "tempismo" dell'entry).
Nessuna modifica al sistema, nessun intervento proposto come deciso. Serve a
mettere il consulente in condizione di valutare se vale la pena costruire la
validazione formale descritta in fondo.

## 1. L'intuizione da discutere

Tema: il vero leva-cambia-gioco potrebbe non essere *filtrare* il regime ma il
**tempismo** con cui il sistema entra. L'ipotesi e' che il sistema entri quando
il movimento e' gia' avvenuto, cioe' "in coda" al movimento, prendendosi il
ritracciamento invece dello sviluppo.

Va distinta subito in **due meccanismi diversi**, perche' portano a interventi
opposti:

- **(a) Lentezza di cadenza / rilevazione**: lo scanner gira ogni ora
  (`morning_scan`, cron `5 7-22` Europe/Rome). Tra la formazione del setup e la
  lettura del sistema possono passare fino a ~60 minuti. In regime volatile e'
  molto. Risolverlo significa passare da snapshot orari a logica event-driven
  (trigger su livello / conferma di breakout): cambio architetturale, non un
  tweak.
- **(b) Entry su movimento gia' esteso / maturo**: a parita' di cadenza,
  entriamo quando il movimento direzionale e' gia' largamente avvenuto. E' un
  problema di *selezione*, vive dentro la logica di scoring, ed e' codificabile
  come **feature**.

La tesi di questo documento, dai dati, e' che il segnale forte sta su **(b)**,
mentre **(a)** e' gia' stato in parte indebolito da un'analisi precedente
(sezione 2.1).

## 2. Cosa sappiamo gia': tre analisi che convergono

### 2.1 `sprint2-reactivity-analysis.md` (2026-05-22) — la versione "lentezza"

Ha testato direttamente "il sistema entra in ritardo" rigirando la lettura del
modello a passi di 4h da T-24h a T (6 trade, replay con `jobs/replay_signal.py`).
Esiti rilevanti:

- **Il ritardo di rilevazione NON e' confermato**: dove il setup direzionale
  esiste, il sistema lo vede ore prima (T-12h, T-16h, T-24h) a score stabile,
  non all'ultimo momento. I casi che *sembrano* tardivi erano setup non ancora
  validi prima (asset ipercomprato per tutta la finestra, il long si forma solo
  quando l'RSI rientra). Quindi il meccanismo (a) nella sua forma "vediamo il
  setup tardi" e' debole.
- **Finding inatteso e netto**: sui 3 LOSS del campione, il codice attuale
  rigirato a T NON avrebbe generato il signal (RSI in ipercomprato, "skip"). Il
  codice vecchio entrava su **setup vicini all'esaurimento** (RSI 75+). Citazione
  dal doc: *"Non e' lentezza, e' entrata su un movimento gia' concluso."* Questa
  e' esattamente la forma (b) dell'ipotesi.
- Restava aperto l'**effetto soglia**: direzione vista presto, score fermo
  ~6.5-6.8 per 12-24h, scatta a 7 solo quando il movimento si conferma. Non
  dimostrabile con lo strumento di allora (la lettura isolata non raggiunge 7).

Limite forte di quell'analisi: 6 trade, codice di replay diverso da quello che
ha generato i signal. La raccomandazione era rifare la misura sui trade reali
di Fase 3. Quei trade sono il sample 38-58 usato qui sotto.

### 2.2 `sprint2-mfe-analysis.md` (2026-06-03) — il problema e' all'entry

7 LOSS su 9 non hanno **mai** superato 0.5R: sono andati contro quasi subito.
*"Non sono casi di give-back ma di entry sbagliati / reattivita'."* Conferma che
il difetto vive a monte dell'apertura, non nella gestione (trailing/exit). Il
give-back vero (fascia 1.0-1.5R) riguarda solo 2 trade.

### 2.3 `sprint3.5-entry-diagnosis.md` (2026-06-06) — la firma del regime

Sul sample 38-58 (21 trade chiusi, 6 WIN / 14 LOSS / 1 BE), definisce:

- **RAPID_LOSS** (n=8): pnl < 0 e `peak_R` < 0.5 (mai andati a favore). id 38,
  39, 41, 46, 47, 50, 53, 56.
- **DEVELOPED** (n=13): il resto.

Trova che i rapid loss entrano in regimi ~2x piu' volatili (atr_pct mediana 1.32
vs 0.70; bb_width 5.60 vs 2.89) e su asset piu' deboli nella giornata
(`daily_pct_change` mediana -0.53 vs +0.41; 5/8 su down-day contro 3/13). E che
**lo score LLM non discrimina** (7.25 vs 7.00, anzi i rapid loss leggermente piu'
alti). Nota gia' allora: i rapid loss sono spesso *"short che inseguono un
ribasso gia' maturo e poi rimbalzano contro"*.

### 2.4 `sprint4-troncone2-feature-sim.md` (2026-06-09) — perche' filtrare non basta

Ha mostrato che un filtro di volatilita' non e' la strada: i **2 TP piu' grandi**
(#42 +10.85, #49 +9.46) vivono in **alta volatilita'**, agli stessi valori
`atr_pct` dei rapid loss (WIN fino a 2.15, LOSS fino a 2.19, sovrapposti al
centesimo). Un filtro che taglia la volatilita' alta taglia anche i due TP
maggiori. Conclusione: serve *"un segnale diverso da volatilita'/posizione"*.

## 3. La rilettura nuova: l'"estensione del movimento" alla direzione del trade

Le tre analisi sopra puntano tutte nella stessa direzione senza nominarla: il
discriminante non e' *quanto e' volatile* il momento, ma **quanto del movimento
nella direzione del trade e' gia' avvenuto quando entriamo**. La volatilita'
separa solo perche' e' correlata (i movimenti estesi sono volatili), ma e' la
proiezione sbagliata.

Proxy ex-ante costruibile dai dati gia' presenti: l'estensione firmata per la
direzione del trade,

```
chasing = daily_pct_change            se LONG
chasing = - daily_pct_change          se SHORT
```

Valore alto positivo = l'asset si e' gia' mosso molto **nella mia direzione**
prima dell'entry, cioe' sto inseguendo un movimento maturo. Valore ~0 o negativo
= entro mentre il movimento e' ancora da venire (entry "fresco").

Calcolato sui 21 trade dalla tabella di `sprint3.5-entry-diagnosis.md`
(`daily_pct_change`, `peak_R` proxy da candele, esito reali), ordinato per
`chasing` decrescente:

| id | asset | dir | grp | daily_pct_change | chasing | peak_R | pnl |
|----|-------|-----|-----|------------------|---------|--------|-----|
| 38 | Brent | short | RAPID | -5.05 | **+5.05** | 0.28 | -0.88 |
| 46 | Brent | short | RAPID | -3.38 | **+3.38** | 0.45 | -2.52 |
| 44 | Brent | long | DEV | +2.69 | **+2.69** | 0.68 | -2.44 |
| 51 | Brent | long | DEV | +2.39 | **+2.39** | 1.16 | -0.11 |
| 56 | Brent | long | RAPID | +2.07 | **+2.07** | 0.05 | -3.31 |
| 52 | Bitcoin | short | DEV | -1.92 | +1.92 | 2.08 | **+2.01** |
| 39 | Nasdaq | long | RAPID | +1.44 | +1.44 | 0.38 | -0.79 |
| 43 | Gold | long | DEV | +1.25 | +1.25 | 0.52 | -1.93 |
| 53 | Gold | long | RAPID | +1.16 | +1.16 | 0.08 | -1.59 |
| 58 | Nasdaq | short | DEV | -0.88 | +0.88 | 2.10 | **+6.77** |
| 50 | Brent | short | RAPID | -0.80 | +0.80 | 0.11 | -2.98 |
| 57 | Gold | long | DEV | +0.73 | +0.73 | 0.90 | -1.61 |
| 55 | Brent | long | DEV | +0.68 | +0.68 | 1.09 | -0.06 |
| 47 | Gold | short | RAPID | -0.67 | +0.67 | -0.09 | -4.00 |
| 48 | Nasdaq | long | DEV | +0.41 | +0.41 | 1.41 | **+2.82** |
| 40 | Nasdaq | long | DEV | +0.40 | +0.40 | 1.12 | **+4.16** |
| 45 | Nasdaq | long | DEV | +0.28 | +0.28 | 1.27 | 0.00 |
| 54 | Nasdaq | long | DEV | +0.25 | +0.25 | 0.77 | -1.10 |
| 49 | Brent | short | DEV | -0.02 | +0.02 | 2.17 | **+9.46** |
| 41 | Gold | long | RAPID | -0.39 | -0.39 | 0.13 | -0.19 |
| 42 | Brent | short | DEV | +0.81 | **-0.81** | 2.93 | **+10.85** |

### Cosa dice la tabella

1. **In cima (chasing >= 2) ci sono 5 trade, tutti in perdita** (#38, #46, #44,
   #51, #56), totale -9.26. I 5 entry "piu' inseguitori" non producono un solo
   WIN.
2. **I 2 TP piu' grandi sono i 2 entry meno inseguitori**: #42 (chasing -0.81,
   +10.85) e #49 (chasing +0.02, +9.46) stanno in fondo alla lista. Entrano
   *prima* che il movimento sia avvenuto. Sono gli stessi due trade che avevano
   affossato il filtro di volatilita' del troncone 2: stessa alta volatilita',
   ma tempismo opposto rispetto ai rapid loss.
3. **5 WIN su 6 hanno chasing <= 0.88**; i 2 maggiori <= 0.02. L'unica eccezione
   e' #52 (Bitcoin short, chasing +1.92, +2.01), il WIN piu' piccolo.
4. Mediana `chasing`: RAPID 1.30 vs DEVELOPED 0.68. Stesso ordine di separazione
   della volatilita' (atr 1.32 vs 0.70), ma su una dimensione che **non taglia i
   TP grandi**, a differenza della volatilita'.

### Il cluster pulito: Brent

Brent e' l'asset dei TP piu' grandi e dei loss piu' pesanti, quello che ha
mandato a vuoto il troncone 2. Isolandolo (9 trade), `chasing` separa quasi
perfettamente:

| esito | id | chasing |
|-------|----|---------|
| WIN +10.85 | 42 | -0.81 |
| WIN +9.46 | 49 | +0.02 |
| LOSS / BE | 50, 55, 51, 56, 44, 46, 38 | +0.68 ... +5.05 |

I due Brent vincenti sono gli unici due con `chasing <= 0`. Tutti gli altri
inseguono (`chasing > 0.6`) e nessuno e' un WIN. Stesso asset, stessa direzione
prevalente (short), stessa volatilita' di fondo: cio' che separa e' **quando**
entri rispetto al movimento.

### Onesta' sui limiti del segnale

- **Gold non separa**: #41 ha chasing -0.39 ed e' un rapid loss; #43/#57 hanno
  chasing positivo e sono "developed" ma comunque in perdita. Pero' Gold non
  produce **nessun** WIN nel campione: e' verosimilmente un problema di asset a
  se', non di tempismo. Da trattare separatamente.
- **#52 (Bitcoin)** e' un controesempio (chasing alto, WIN), anche se il WIN piu'
  piccolo.
- `chasing` basso e' quasi **necessario** ma non **sufficiente**: nella zona
  bassa cadono anche piccole perdite (#54 Nasdaq -1.10, #41 Gold -0.19). Entrare
  "fresco" e' dove vivono tutti i WIN e in particolare i grandi, ma non li
  garantisce.
- `daily_pct_change` e' un proxy grezzo di "estensione": misura il movimento
  sull'intera giornata, non dal punto di trigger del setup. Una metrica migliore
  sarebbe l'estensione in multipli di ATR dal livello di innesco (vedi sezione
  5).

## 4. La sintesi che lega tutto

La volatilita' del troncone 2, l'esaurimento RSI della reactivity-analysis e il
`peak_R ~ 0` dei rapid loss sono tre facce della stessa cosa: **stiamo entrando
in coda a movimenti gia' estesi**. Tre conferme indipendenti:

- *reactivity (mag)*: qualitativa, "entry su movimento gia' concluso", RSI 75+.
- *entry-diagnosis (giu)*: i rapid loss sono short che inseguono un ribasso
  maturo; lo score non lo vede.
- *questa rilettura*: la metrica `chasing` separa i WIN (soprattutto i grandi)
  dai rapid loss meglio della volatilita', e non taglia i TP.

Punto importante per il consulente: la rifinitura RSI di Fase 2a-bis (che la
reactivity-analysis sperava avesse gia' mitigato il problema) **non lo ha
chiuso**. Il sample 38-58 e' gia' post-rifinitura, eppure conta ancora 8 rapid
loss. L'esaurimento non si vede solo dall'RSI: un Brent short su -5% di giornata
puo' avere RSI non estremo ma essere comunque un inseguimento. Questo motiva un
segnale di estensione dedicato, indipendente dall'RSI.

E lega anche il meccanismo (a) al (b): l'**effetto soglia** lasciato aperto dalla
reactivity (score fermo sotto 7 finche' il movimento non si conferma) e' un modo
in cui il sistema, pur "vedendo" presto, finisce per *eseguire* tardi. Non e'
cadenza lenta, e' che la conferma che porta lo score a 7 spesso coincide con
l'estensione del movimento. Da verificare.

## 5. Cosa NON e' ancora stato fatto (la validazione da costruire)

Questa nota e' una rilettura di dati esistenti, non una simulazione nuova. Per
decidere se costruire qualcosa serve, in stile "simula prima di costruire" (come
per il trailing):

1. **Metrica di estensione migliore di `daily_pct_change`**: distanza in multipli
   di ATR tra il prezzo di entry e il livello di trigger/breakout del setup. Va
   ricostruita dalle candele; misura "di quanti ATR il movimento e' gia' corso".
2. **Rifare la separazione su Sprint 3**, dove esiste `intra_trade_extreme`
   nativo (dal 2026-06-05): `peak_R` pulito invece del proxy da candele. Verifica
   se la relazione `chasing -> peak_R basso` regge fuori dal sample 38-58.
3. **Test dell'effetto soglia (meccanismo a)**: rigirare il **panel** completo
   (non la lettura isolata, che era il limite della reactivity) attorno a T per
   vedere se lo score attraversa 7 in coincidenza dell'estensione del movimento.
4. Solo se 1-3 confermano, valutare l'intervento. Due opzioni, in ordine di
   reversibilita':
   - **Via B (feature contestuale all'LLM)**: dare al modello "estensione del
     movimento alla direzione proposta" e istruirlo a penalizzare gli entry su
     movimento gia' maturo. Coerente con la raccomandazione del troncone 2 (Via B
     e non filtro hard). Reversibile, testabile in shadow.
   - **Via A architetturale (event-driven)**: solo se il meccanismo (a)/effetto
     soglia risulta dominante. Pesante, da valutare a parte.

## 6. Domande per il consulente

1. La metrica `chasing` (estensione firmata) e' un proxy sensato di "entry in
   coda al movimento", o e' confusa da fattori che non vediamo (gap di apertura,
   sessione, mean-reversion vs trend per asset)?
2. Meglio l'estensione sulla giornata (`daily_pct_change`) o in ATR dal trigger?
   C'e' una formulazione standard nella letteratura che converrebbe adottare?
3. Il segnale "fresco vs inseguimento" e' meglio dato all'LLM come feature (Via
   B) o vale come pre-filtro/penalita' deterministica nonostante il rischio di
   tagliare i breakout buoni (lezione del troncone 2)?
4. Su Brent il segnale e' netto ma Brent e' strutturalmente volatile e
   trend-friendly: c'e' il rischio che `chasing` funzioni solo su asset cosi' e
   non su Gold (mean-reverting)? Conviene una logica asset-specifica?

## 7. Limiti (da tenere in testa)

Sample 21 trade (di cui Brent 9): direzioni, non significativita'. `peak_R` e'
proxy da candele 5m/15m sul sample 38-58. `chasing` derivato da
`daily_pct_change`, proxy grezzo dell'estensione. Esiti P&L influenzati anche da
trailing e chiusure manuali, non solo dall'entry. Gold e Bitcoin sono parziali
controesempi. Tutto da riconfermare su Sprint 3 con dati nativi prima di
qualunque intervento.
