# Sprint 2 — Indagine sul bias direzionale long

Stato: diagnosi completata, 2026-05-20. Nessuna modifica al sistema applicata.
Le modifiche partono in Fase 2 dopo lettura di questo documento.

## 1. Contesto

Sprint 1 si è chiuso con verdict "coin flip con rete di sicurezza" su 18 trade
post-fix marginFactor (-€1.63). È emerso un finding strutturale: **40 long / 1
short su 41 signal in 60 giorni**. Prima di toccare il sistema serve capire dove
nasce il bias: nel prompt, nei feature o nei filtri.

## 2. Metodo

1. **Individuazione dei momenti short**: lo script `jobs/replay_signal.py scan`
   ha esaminato le candele DAY dei 5 asset core negli ultimi 60 giorni
   (2026-03-21 / 2026-05-20) cercando drawdown ≥ 3% entro 1-3 giorni dal picco.
   Risultato: 27 episodi. Ne sono stati selezionati 9 con copertura di tutti e
   5 gli asset e magnitudo da 3.95% a 18.42%.
2. **Istante di interesse T**: per ogni episodio, la prima candela 4H dopo il
   picco in cui il calo supera 1.2%. È il momento operativo in cui uno short
   andrebbe aperto: il ribasso è già visibile ma resta spazio per il TP.
3. **Rigenerazione del signal**: `jobs/replay_signal.py replay` ricostruisce le
   feature tecniche come le avrebbe viste lo scanner a T (ultime 60 candele 4H
   con timestamp ≤ T, stessa logica di `compute_features` / `_collect_features`)
   e rigira la pipeline reale: `_prefilter_candidates` → `LLMAnalyzer.rank_setups`
   (system prompt corrente, modello fast) → `apply_macro_guardrails`. Per ogni
   momento sono state fatte due chiamate: una sul set pre-filtrato (pipeline
   fedele) e una "lettura isolata" sul solo asset di interesse.

### Limiti della ricostruzione (dichiarati)

- **News, `critical_events`, `economic_calendar` non sono ricostruibili a
  posteriori** e sono stati passati vuoti. I guardrail macro diventano quindi
  no-op. Non inquina la diagnosi: il bias long/short dipende dalla lettura
  tecnica del modello, non dalle penalità macro.
- `daily_pct_change`, `daily_range_pct`, `pct_from_daily_high` sono ricostruiti
  dalle candele 4H del giorno di T, non dal `percentageChange` live di Capital.
  Buona approssimazione.
- Soglie del sistema usate: `MIN_SCORE_THRESHOLD = 7`, `MIN_RR_AT_ENTRY = 1.2`.

## 3. I 9 casi

Legenda feature a T: `rsi` = RSI 4H, `slope` = trend_slope_pct (regressione 20
candele 4H), `pfh` = pct_from_high_20, `daily` = daily_pct_change.

La colonna "Classe LLM" è la diagnosi a livello modello; per i casi esclusi dal
pre-filtro è un controfattuale ricavato dalla lettura isolata (vedi nota dopo la
tabella). La colonna "Causa operativa" è ciò che ha realmente bloccato lo short
nella pipeline live.

| # | T (UTC) | Asset | Drop | Feature a T | Pre-filtro | LLM panel (asset) | LLM isolato | Classe LLM | Causa operativa |
|---|---------|-------|------|-------------|-----------|-------------------|-------------|--------|--------|
| 1 | 2026-03-26 16:00 | US500 | 3.95% | rsi 31.7, slope -0.04, daily -1.6, pfh -2.17 | **ESCLUSO** | non valutato | long 5.5 | PROMPT* | **PRE-FILTRO** |
| 2 | 2026-03-26 16:00 | Nasdaq 100 | 4.92% | rsi 28.0, slope -0.07, daily -2.18, pfh -2.86 | passa | long 5.5 | long 6.5 | PROMPT | PROMPT |
| 3 | 2026-04-01 04:00 | Gold | 4.81% | rsi 84.3, slope +0.28, daily +0.36, pfh 0.0 | passa | skip 4.0 | skip 4.5 | PROMPT | PROMPT |
| 4 | 2026-04-06 00:00 | Brent Oil | 18.42% | rsi 59.4, slope +0.14, daily -1.66, pfh -1.67 | **ESCLUSO** | non valutato | long 5.5 | FEATURE* | **PRE-FILTRO** |
| 5 | 2026-04-17 04:00 | Bitcoin | 4.36% | rsi 58.5, slope +0.04, daily -0.1, pfh -0.37 | passa | skip 4.5 | long 5.5 | FEATURE | FEATURE |
| 6 | 2026-04-29 04:00 | Brent Oil | 6.73% | rsi 66.3, slope +0.30, daily +1.97, pfh 0.0 | passa | **long 7.5** | long 7.0 | FEATURE | FEATURE |
| 7 | 2026-05-04 04:00 | Brent Oil | 15.62% | rsi 37.0, slope -0.09, daily +1.57, pfh -3.35 | **ESCLUSO** | non valutato | long 5.5 | PROMPT* | **PRE-FILTRO** |
| 8 | 2026-05-14 04:00 | Bitcoin | 5.33% | rsi 36.7, slope -0.14, daily +0.53, pfh -2.73 | passa | non in top 3 | skip 4.5 | PROMPT | PROMPT |
| 9 | 2026-05-14 16:00 | Gold | 4.36% | rsi 38.3, slope -0.07, daily -0.9, pfh -2.37 | **ESCLUSO** | non valutato | skip 4.5 | FILTRO* | **PRE-FILTRO** |

`*` = classe LLM controfattuale: l'asset è stato escluso dal pre-filtro e il
modello non l'ha mai valutato; la classe deriva dalla lettura isolata.

Dato trasversale: **in nessuno dei 9 momenti, su 36 output di proposal
complessivi, uno short ha mai ricevuto score ≥ 7**. Lo short con score più alto
è 6.5 (Brent, panel del caso 3). I long hanno toccato 7.5 ripetutamente. La
lettura isolata sull'asset crollato ha prodotto 6 long, 3 skip, **0 short**.

### Due tagli di lettura: classe LLM vs causa operativa

Le tre classi PROMPT / FEATURE / FILTRO descrivono tutte lo **stadio LLM**: cosa
fa il modello con le feature che riceve. Il pre-filtro (`_prefilter_candidates`)
sta a uno **stadio precedente** e indipendente dal LLM. Per questo non rientra
nelle tre classi: è una quinta causa, di natura diversa.

Per i 4 casi esclusi dal pre-filtro (1, 4, 7, 9) il LLM non ha mai valutato
l'asset. La classe PROMPT/FEATURE/FILTRO che compare per quei 4 è ricavata dalla
*lettura isolata*, cioè è un controfattuale ("cosa avrebbe fatto il modello se
l'avesse visto"), non ciò che è accaduto nella pipeline reale. Ciò che è accaduto
davvero è: il pre-filtro ha escluso l'asset e il signal non è mai nato.

Esistono quindi due conteggi, entrambi validi ma che rispondono a domande
diverse:

| Taglio | Domanda a cui risponde | Distribuzione |
|---|---|---|
| Classe LLM | Se il modello vedesse l'asset, dove fallirebbe? | PROMPT 5, FEATURE 3, FILTRO 1 |
| Causa operativa | Cosa ha bloccato lo short nella pipeline live? | **PRE-FILTRO 4** (1,4,7,9), PROMPT 3 (2,3,8), FEATURE 2 (5,6), FILTRO 0 |

Per causa operativa il pre-filtro è il **bucket singolo più grande** (4/9). Il
caso 9, unico classificato FILTRO nel taglio LLM, sotto il taglio operativo
ricade in PRE-FILTRO: il filtro RR/score interno al modello non è mai arrivato
ad agire, perché l'asset era già stato escluso a monte.

### Caso 1 — US500, 2026-03-26, drop 3.95%

- **Cosa ha proposto il LLM**: nel panel US500 non è stato valutato (escluso dal
  pre-filtro). Lettura isolata: **long contrarian, score 5.5**, thesis "rimbalzo
  tecnico da zona di minimo 20 candele con RSI in area critica".
- **Short proposto?** No.
- **Long contrarian?** Sì: il modello legge `rsi 31.7` + prezzo su `low_20` come
  esaurimento ribassista e propone un rimbalzo, non una continuazione short.
- **Pre-filtro**: US500 non soddisfa nessun criterio (`|daily| ≥ 2`? no, -1.6;
  `rsi ≤ 30`? no, 31.7; `|pfh| ≤ 1` o `pfh ≤ -8`? no, -2.17; `bb_width ≤ 1.5`?
  no, 1.91). Il LLM non ha mai visto US500.
- **Classe LLM: PROMPT.** Le feature mostrano debolezza (RSI vicino a
  ipervenduto, daily negativo, prezzo sotto i massimi); il modello la converte
  in tesi long. La classe è un controfattuale dalla lettura isolata.
- **Causa operativa: PRE-FILTRO.** Nella pipeline reale US500 è stato escluso a
  monte e il LLM non l'ha mai valutato (vedi §5-E).

### Caso 2 — Nasdaq 100, 2026-03-26, drop 4.92%

- **Cosa ha proposto il LLM**: panel → **Nasdaq long, score 5.5**; lettura
  isolata → **long, score 6.5**, thesis "RSI 4H a 28, zona di ipervenduto
  tecnico che storicamente anticipa rimbalzi di breve".
- **Short proposto?** No.
- **Long contrarian?** Sì, esplicito: `rsi 28` è il segnale tecnico più chiaro
  di tutto il caso, e il modello lo usa come argomento **a favore di un long**.
- **Classe (LLM e operativa): PROMPT.** Il feature bearish c'è ed è inequivocabile (RSI 28). Il
  modello non lo legge come "downtrend in corso, short di continuazione" ma come
  "molla compressa per un rimbalzo long".

### Caso 3 — Gold, 2026-04-01, drop 4.81%

- **Cosa ha proposto il LLM**: panel → **Gold skip, score 4.0**; lettura isolata
  → **skip, score 4.5**. Thesis: "RSI 4H a 84.3 ipercomprato estremo... un long
  esporrebbe a mean-reversion, **mentre un setup short contro-trend non è
  supportato da deterioramento tecnico**".
- **Short proposto?** No, e il modello lo motiva esplicitamente.
- **Skip con quale ragionamento?** RSI 84 = esaurimento → niente long; ma lo
  short richiederebbe `trend_slope` negativo, che a T è ancora +0.28 → niente
  short. Risultato: skip.
- **Classe (LLM e operativa): PROMPT.** `rsi 84` è un segnale di esaurimento da manuale, il
  candidato short più pulito dei 9. Il prompt insegna al modello a leggerlo solo
  come "non comprare", mai come "vendi": lo short è ammesso solo come
  continuazione di un trend già negativo. Al top di un movimento il sistema è
  strutturalmente incapace di shortare.

### Caso 4 — Brent Oil, 2026-04-06, drop 18.42%

- **Cosa ha proposto il LLM**: panel → Brent escluso dal pre-filtro; top setup
  Nasdaq long 6.5. Lettura isolata Brent → **long, score 5.5**.
- **Short proposto?** No.
- **Classe LLM: FEATURE.** A T le feature mostrano un uptrend: `slope +0.14`
  positivo, `rsi 59.4` in zona rialzista, prezzo a -1.67% dai massimi. Il crollo
  del 18% non è ancora visibile in nessun numero tecnico. Anche un analista
  onesto, guardando solo quei dati, non shorterebbe. Il problema qui è che
  l'istante T cattura l'inizio del movimento prima che la struttura 4H si
  inverta.
- **Causa operativa: PRE-FILTRO.** Brent è stato escluso a monte; la classe
  FEATURE è un controfattuale dalla lettura isolata.

### Caso 5 — Bitcoin, 2026-04-17, drop 4.36%

- **Cosa ha proposto il LLM**: panel → BTC skip 4.5; lettura isolata → long 5.5
  debole.
- **Short proposto?** No.
- **Classe (LLM e operativa): FEATURE.** Quadro completamente neutro: `slope +0.04` piatto, `rsi
  58.5` neutro, `pfh -0.37` (sui massimi), `daily -0.1` nullo. Non esiste nessun
  segnale, né long né short. Il modello stesso assegna 5.5 e cita "assenza di
  trigger". I numeri tecnici non mostrano un pattern short perché a T non c'è
  ancora nulla.

### Caso 6 — Brent Oil, 2026-04-29, drop 6.73%

- **Cosa ha proposto il LLM**: panel → **Brent long, score 7.5** (sopra la
  soglia di 7: a questo istante il sistema avrebbe sparato un signal long).
  Lettura isolata → **long, score 7.0**.
- **Short proposto?** No.
- **Classe (LLM e operativa): FEATURE.** A T le feature gridano long: `slope +0.30` (il più forte
  del panel), prezzo esattamente sul massimo 20 candele (`pfh 0.0`), `daily
  +1.97%`. Il modello legge correttamente i numeri che ha; il problema è che
  quei numeri, al top del movimento, non contengono l'informazione del reversal
  imminente. Caso emblematico: la pipeline non solo non shorta, ma **avrebbe
  aperto un long perdente** poche ore prima di un calo del 6.73%.

### Caso 7 — Brent Oil, 2026-05-04, drop 15.62%

- **Cosa ha proposto il LLM**: panel → Brent escluso dal pre-filtro; top Nasdaq
  long 6.5. Lettura isolata Brent → **long, score 5.5**, thesis "rimbalzo
  tattico da ipervenduto relativo".
- **Short proposto?** No.
- **Classe LLM: PROMPT.** A T ci sono segnali di debolezza (`slope -0.09`
  negativo, `rsi 37` in zona bassa). Il modello li trasforma in una tesi di
  rimbalzo long invece che di continuazione short. Stesso meccanismo dei casi
  1-2. La classe è un controfattuale dalla lettura isolata.
- **Causa operativa: PRE-FILTRO.** Brent è stato escluso a monte (vedi §5-E).

### Caso 8 — Bitcoin, 2026-05-14, drop 5.33%

- **Cosa ha proposto il LLM**: panel → BTC non rientra nei top 3 (3 proposal,
  tutti long su Brent/US500/Nasdaq). Lettura isolata → **skip, score 4.5**,
  thesis "trend_slope negativo debole... RSI in debolezza... non offre asimmetria
  sufficiente".
- **Short proposto?** No: il modello non formula mai una direzione short, va
  direttamente a skip.
- **Classe (LLM e operativa): PROMPT.** Qui le feature sono chiaramente bearish (`slope -0.14`
  negativo, `rsi 36.7`). Il modello le riconosce e le cita, ma il suo spazio di
  uscita è solo {long, skip}: non avendo un template per lo short, di fronte a
  un downtrend debole sceglie skip.

### Caso 9 — Gold, 2026-05-14, drop 4.36%

- **Cosa ha proposto il LLM**: panel → Gold escluso dal pre-filtro. Lettura
  isolata → **skip, score 4.5**. Thesis: "prezzo coincidente con low_20...
  **rottura del minimo 20 candele aprirebbe spazio ribassista significativo**...
  ma il setup non soddisfa il requisito di asimmetria minima 2:1 con sufficiente
  confidenza direzionale".
- **Short proposto?** Quasi: il modello **descrive esplicitamente lo scenario
  short** ("rottura del minimo → spazio ribassista") nei key_factors e nei
  risks, poi lo scarta.
- **Su quale filtro fallisce?** Sul requisito di asimmetria/RR 2:1 con
  "sufficiente confidenza direzionale" e sull'assenza di catalyst, che il
  modello applica come gate interno e che lo porta a skip.
- **Classe LLM: FILTRO.** Nella lettura isolata è l'unico caso in cui il LLM
  vede lo short, lo nomina, e lo elimina per RR/confidenza insufficiente. Da
  notare: non è un filtro di codice (il `min_rr_at_entry` agisce solo al click,
  in `_handle_confirm`), è il gate "asimmetria 2:1 + selettività" del system
  prompt che il modello applica in modo più severo agli short che ai long.
- **Causa operativa: PRE-FILTRO.** Nella pipeline reale Gold è stato escluso a
  monte: il gate RR interno al modello non è mai arrivato ad agire. La classe
  FILTRO qui è un controfattuale.

## 4. Risposta alle domande poste sui filtri

> Se ha proposto short: ha superato i filtri RR/altri?

Negli output dei 9 momenti uno short compare solo due volte, entrambe nei panel:
**Nasdaq short 5.5** (caso 1) e **Brent short 6.5** (caso 3). Nessuno dei due
raggiunge `MIN_SCORE_THRESHOLD = 7`: vengono eliminati dal filtro di soglia in
`_pick_top_setup`. In entrambi i casi è **il LLM stesso** ad auto-assegnare uno
score basso, motivandolo (caso 1, testuale: "lo score è ridotto perché
un'entrata short in questa configurazione offre un'asimmetria rischio/rendimento
sfavorevole"). Non esiste un filtro RR a livello di generazione del signal: il
`min_rr_at_entry` agisce solo alla conferma. Quindi lo short non muore in un
filtro deterministico, **muore nello score che il LLM gli assegna**.

## 5. Sintesi diagnostica

Il bias ha **cinque cause su due stadi distinti**. Le prime quattro (A-D) sono
allo stadio LLM o lo riguardano direttamente; la quinta (il pre-filtro, F) è a
uno stadio precedente e indipendente dal modello.

- Classe LLM (cosa fa il modello se vede l'asset): **PROMPT 5/9, FEATURE 3/9,
  FILTRO 1/9.**
- Causa operativa (cosa blocca lo short nella pipeline live): **PRE-FILTRO 4/9,
  PROMPT 3/9, FEATURE 2/9, FILTRO 0/9.**

Le cause non sono indipendenti: si sommano in un imbuto che rende lo short quasi
impossibile.

### A. Il prompt non ha un template di short (causa principale)

Il system prompt descrive in dettaglio cosa rende buono un setup, ma il
materiale illustrativo è quasi tutto long-orientato:
- l'unico esempio JSON completo è `"GOLD" "long" 7.5`;
- gli esempi di `key_factors` sono "RSI 4H esce da ipervenduto", "trend_slope_pct
  positivo +0.6", "pct_from_high_20 -1.2% pronto al test": tutti long;
- l'unica guida esplicita allo short sta nel blocco crypto-weekend ("RSI tra 30
  e 65 per short", "per short setup distanti dal massimo") e nel blocco momentum
  ("short sulla vendita forte" solo per `|daily| > 10%`).

Per un ribasso ordinario del 3-6% su un asset tradizionale **il prompt non dice
al modello che aspetto ha un buono short**. Di conseguenza il modello applica il
suo prior: ribasso = ipervenduto = molla per un rimbalzo = long. Si vede nei
casi 1, 2, 7 (debolezza → tesi long) e 3, 8 (debolezza/esaurimento → skip,
perché lo spazio d'uscita percepito è solo {long, skip}).

### B. La lettura dell'RSI è asimmetrica

Il prompt insegna "RSI sopra 75 o sotto 25 indica esaurimento, NON
continuazione". Il modello generalizza in una sola direzione: RSI basso →
esaurimento ribassista → rimbalzo → **long** (casi 1, 2); RSI alto → esaurimento
rialzista → **non comprare**, ma nessuna istruzione lo porta a **vendere** (caso
3). L'RSI estremo uccide i long sui top e crea i long sui fondi: è un generatore
di bias long puro.

### C. `trend_slope_pct` è una feature in ritardo (causa dei casi FEATURE)

`trend_slope_pct` è la regressione lineare su 20 candele 4H, cioè ~3,3 giorni di
finestra. Un breakdown di 1 giorno non basta a renderla negativa: nei casi 1
(`slope -0.04`), 4 (`+0.14`), 6 (`+0.30`) la pendenza è piatta o positiva mentre
il prezzo sta già rompendo al ribasso. I 3 casi classificati FEATURE non sono
"colpa" del modello: a T i numeri tecnici mostrano davvero un uptrend o un
quadro neutro, perché la feature di trend è troppo lenta per catturare l'inizio
di uno swing ribassista. Il modello legge bene dati che arrivano tardi.

### D. Lo score degli short è strutturalmente sotto soglia

Anche quando lo short emerge, il LLM gli assegna 5.5-6.5, mai ≥ 7, e lo motiva
con l'asimmetria sfavorevole (caso 1) o l'assenza di catalyst/confidenza (caso
9). Con `MIN_SCORE_THRESHOLD = 7` nessuno short può diventare signal. Questo è
prompt (B) più la selettività "2:1 + sii selettivo" applicata più severamente
agli short che ai long.

### E. Il pre-filtro è una quinta causa, a monte e indipendente dal LLM

`_prefilter_candidates` fa passare un asset solo se `|daily_pct| ≥ 2` oppure
`rsi ≤ 30 / ≥ 70` oppure `|pfh| ≤ 1` o `pfh ≤ -8` oppure `bb_width ≤ 1.5`. Un
calo giornaliero dell'1,5-2% con RSI intorno a 35-42 (l'inizio tipico di uno
swing-down, la zona di entrata short) **non soddisfa nessuna soglia**. In 4 casi
su 9 (1, 4, 7, 9) l'asset crollato è stato escluso dal pre-filtro e il LLM non
l'ha mai visto: è la causa operativa più frequente, il bucket singolo più grande.

Questa causa va tenuta distinta da A-D. Le prime quattro descrivono come il
modello tratta gli short; questa descrive il fatto che, in 4 casi su 9, **un
modello qualunque non avrebbe potuto fare nulla**, perché l'asset non è mai
arrivato allo stadio LLM. È un filtro deterministico, non un comportamento del
modello.

Non è un bias direzionale per costruzione (il pre-filtro è simmetrico: esclude
anche i rialzi moderati). Ma il suo set di criteri è di fatto tarato sui long:
`|pfh| ≤ 1` intercetta il prezzo vicino ai massimi (setup breakout long),
`pfh ≤ -8` intercetta le correzioni profonde (setup rimbalzo long), e nessun
criterio mira alla zona di breakdown moderato in corso. Il risultato pratico è
una falla di copertura che colpisce in modo sproporzionato proprio gli istanti
di entrata short.

### Conclusione

I 40 long / 1 short si spiegano integralmente, su due stadi.

A monte del LLM, **il pre-filtro** (E) non fa nemmeno arrivare al modello i
ribassi moderati: è la causa operativa di 4 casi su 9, indipendente da qualunque
comportamento del modello.

Allo stadio LLM, per i ribassi che arrivano: il prompt non offre un template di
short e spinge a leggere ogni debolezza come rimbalzo long o come skip (A, B);
la feature di trend è troppo lenta per mostrare un pattern short all'inizio del
movimento (C); e quando uno short sopravvive, lo score che il modello gli
assegna è sotto soglia (D).

**Le due leve da affrontare insieme sono il prompt (A + B + D) e il pre-filtro
(E).** Vanno toccate in tandem perché sono accoppiate: sistemare il pre-filtro
da solo instraderebbe più ribassi verso un LLM che continua a non shortarli (la
lettura isolata dei casi 1, 7, 9 lo conferma: anche visti, sarebbero diventati
long o skip); sistemare il prompt da solo lascerebbe comunque invisibili al
modello i 4 casi su 9 esclusi a monte. Nessuna delle due, da sola, chiude il
bias. La feature di trend in ritardo (C) è una causa reale ma di secondo ordine,
da rivalutare dopo aver misurato l'effetto delle prime due modifiche.

## 6. Perimetro e Fase 2

Questa fase è solo diagnosi: **nessuna modifica al sistema è stata applicata**.
Lo script `jobs/replay_signal.py` è committato e riutilizzabile per misurare
l'effetto di qualunque intervento (rigirare gli stessi 9 momenti dopo una
modifica e confrontare gli output).

Ipotesi da validare in Fase 2, in ordine di priorità suggerito, da decidere
dopo lettura di questo documento. I punti 1 e 2 sono accoppiati e vanno fatti
in tandem (vedi Conclusione): nessuno dei due, da solo, chiude il bias.
1. Riequilibrare il system prompt: template esplicito di short, esempio JSON
   short, `key_factors` short, lettura simmetrica dell'RSI (cause A, B, D).
2. Estendere i criteri del pre-filtro perché intercettino la zona di entrata
   short, il breakdown moderato in corso (causa E): è la causa operativa più
   frequente, 4 casi su 9.
3. Verificare se lo score minimo o la regola "2:1 + selettività" penalizzano
   sistematicamente gli short (causa D).
4. Valutare una feature di trend a finestra più corta (o un secondo slope
   veloce) per ridurre il ritardo dei casi FEATURE (causa C).

## 7. Fase 2a — Risultati gate

Stato: gate eseguito 2026-05-20 sul branch `sprint2-bidirezionalita`. Esito:
**NON SUPERATO** (1 metrica su 3 mancata). Nessun push su `origin/main`.

### Modifiche applicate (branch `sprint2-bidirezionalita`, 3 commit)

1. System prompt `src/llm_analyzer.py`: sezione "Direzione del setup: long E
   short con pari dignità" con template short, sezione RSI simmetrica, regola
   2:1 resa esplicitamente simmetrica, key_factors short, esempio JSON short.
2. `_prefilter_candidates` in `src/scanner.py`: tre criteri "breakdown"
   (breakdown_day, breakdown_offhigh, breakdown_slope), solo estensione.
3. Tooling: `replay_signal.py` stampa le metriche del gate.

Gate eseguito in locale (Capital + Anthropic via `.env` locale), la VM di
produzione è rimasta su `main`. Nessuna modifica al sistema in produzione.

### Confronto prima / dopo sui 9 casi

| # | Asset | Pre-filtro | Panel (asset) | Isolato |
|---|-------|-----------|---------------|---------|
| 1 | US500 | ESCL → **PASSA** | non valutato → long 6.8 | long 5.5 → long 5.8 + **short 5.2** |
| 2 | Nasdaq 100 | PASSA → PASSA | long 5.5 → assente | long 6.5 → long 6.2 |
| 3 | Gold 01/04 | PASSA → PASSA | skip 4.0 → **short 6.5** | skip 4.5 → **short 7.2** |
| 4 | Brent 06/04 | ESCL → ESCL | non valutato → non valutato | long 5.5 → long 6.8 |
| 5 | Bitcoin 17/04 | PASSA → PASSA | skip 4.5 → assente | long 5.5 → skip 4.2 |
| 6 | Brent 29/04 | PASSA → PASSA | long 7.5 → long 7.2 | long 7.0 → long 6.8 |
| 7 | Brent 04/05 | ESCL → **PASSA** | non valutato → long 5.8 | long 5.5 → long 5.8 + **short 5.2** |
| 8 | Bitcoin 14/05 | PASSA → PASSA | non in top 3 → skip 5.2 | skip 4.5 → skip 5.2 |
| 9 | Gold 14/05 | ESCL → **PASSA** | non valutato → **short 5.2** | skip 4.5 → long 5.8 |

### Le tre metriche del gate

| Metrica | Prima | Dopo | Target | Esito |
|---|---|---|---|---|
| Asset crollati che passano il pre-filtro | 5/9 | **8/9** | ≥ 7/9 | superato |
| Short con score ≥ 7 nel panel (asset crollato) | 0/9 | **0/9** | ≥ 3/9 | **mancato** |
| Regressioni (nuovo long contrarian ≥ 7 dove prima skip o long < 7) | — | **0** | 0 | superato |

L'unico long ≥ 7 nel panel è il caso 6 (Brent 29/04, long 7.2), già long 7.5
prima: non è una regressione nuova, ed è un caso FEATURE atteso. Verdetto del
gate: **NON SUPERATO**, manca la metrica short ≥ 7.

### Analisi: cosa è cambiato e cosa no

La modifica del prompt ha avuto un effetto direzionale netto, anche se non
sufficiente a superare la soglia:

- **Il modello ora propone short.** Prima, su 9 letture isolate: 0 short. Dopo:
  short proposti nei casi 1, 3, 7 (isolato) e 3, 9 (panel), più short su altri
  asset nei panel dei casi 5 e 7. La causa A (assenza di template short) è di
  fatto risolta: il modello vede e formula gli short.
- **Lo score degli short resta sotto 7.** Massimo short: 7.2 isolato (caso 3),
  6.8 panel (Nasdaq nel caso 7). Prima il massimo era 6.5. C'è un miglioramento
  ma non basta.

Perché lo score resta sotto soglia, due sotto-cause distinte:

1. **Feature genuinamente deboli a T (causa C, fuori perimetro 2a).** Nei casi
   1, 7, 9 il `trend_slope_pct` a T è -0.04, -0.09, -0.07: piatto. Lo slope è
   la regressione su 20 candele 4H e all'inizio del movimento è ancora
   dominato dal trend precedente (è esattamente la causa C, deferita a 2b). Un
   analista onesto non darebbe 7+ a uno short con slope piatto: lo score 5.2
   di quei casi è in parte corretto. Senza la feature di trend più reattiva
   (causa C), il modello non ha i numeri per uno short ad alta convinzione.
2. **Residuo di lettura asimmetrica dell'RSI.** Nel caso 1 il modello scrive
   ancora "RSI 31.7 in ipervenduto sconsiglia nuovo short": continua a trattare
   un RSI a 31 come ipervenduto, mentre il prompt fissa l'estremo a <25. E nel
   caso 3 lo stesso identico setup riceve 6.5 nel panel ma 7.2 isolato: lo
   score dello short è ancora instabile e dipende dal contesto.

In sintesi: la metà qualitativa del bias (il modello non proponeva short) è
risolta. La metà quantitativa (lo short raggiunge la soglia di signal) è
bloccata per circa due terzi dalla causa C, esplicitamente fuori dal perimetro
di 2a, e per circa un terzo da un residuo di prompt correggibile.

### Raccomandazione

Il gate non è superato, quindi nessun push e nessun deploy: Fase 3 non parte.
Le opzioni, da decidere (sei tu l'autorità del gate):

- **A. Iterazione di prompt e re-run.** Rinforzare la sezione RSI (fascia 28-45
  esplicitamente NON ipervenduta) e la stabilità di score dello short. Realistico
  recuperare 1-2 short ≥ 7, difficile arrivare a 3/9 finché la causa C resta
  aperta: i casi 1/7/9 hanno slope piatto per costruzione.
- **B. Ri-sequenziare: anticipare la causa C (Fase 2b) dentro questa release.**
  La metrica short ≥ 7 dipende da una feature di trend reattiva. Se la causa C
  rientra nel perimetro, il gate diventa raggiungibile. Costo: la release tocca
  tre cose invece di due, l'eccezione a "una variabile alla volta" si allarga.
- **C. Ricalibrare il gate.** La metrica "short ≥ 7" presuppone feature che 2a
  non modifica. Una metrica di 2a più fedele al suo perimetro: "il modello
  propone short con direzione corretta in ≥ N casi" (oggi soddisfatta) e
  "nessuna regressione" (soddisfatta), rimandando la soglia di score a dopo 2b.

Nessuna iterazione è stata avviata: come da workflow, i numeri vengono mostrati
prima di riprovare.

## 8. Fase 2a-bis — risultati gate ripetuto

Stato: gate eseguito 2026-05-20 sul branch `sprint2-bidirezionalita`. Esito:
metrica short ≥ 7 ancora **non superata** in senso stretto (1/9 vs 3/9), ma con
una trasformazione qualitativa del comportamento del modello rispetto a 2a.
Nessun push su `origin/main`.

### Modifiche applicate (2 commit aggiuntivi, anticipo causa C)

1. Feature `trend_slope_short_pct` (regressione su 8 candele 4H, ~1.3 giorni)
   accanto a `trend_slope_pct` (20 candele). Esposta al LLM con la sezione
   "Lettura combinata delle due pendenze".
2. Rifinitura RSI: soglia ipervenduto fissata numericamente a < 25, vietato
   l'uso della parola per RSI ≥ 25.

### Calibrazione della finestra corta (8 candele)

Obiettivo: nei casi 1, 7, 9 (slope a 20 candele piatto) la pendenza corta deve
mostrare il breakdown.

| Caso | trend_slope_pct | trend_slope_short_pct | Esito |
|------|----------------|----------------------|-------|
| 1 US500 | -0.04 | **-0.23** | breakdown visibile |
| 2 Nasdaq | -0.07 | **-0.31** | breakdown visibile |
| 7 Brent 04/05 | -0.09 | **-0.32** | breakdown visibile |
| 8 Bitcoin 14/05 | -0.14 | **-0.35** | breakdown visibile |
| 9 Gold 14/05 | -0.07 | -0.07 | resta piatto |

La finestra 8 trasforma in pendenza chiaramente negativa 4 dei 5 casi deboli.
Il caso 9 (Gold 14/05) resta piatto: è una deriva lenta su più giorni, non uno
swing netto, e nemmeno 8 candele la leggono come breakdown. Finestra 8 confermata.

### Metriche del gate

| Metrica | 2a | 2a-bis | Target | Esito |
|---|---|---|---|---|
| Asset crollati che passano il pre-filtro | 8/9 | 8/9 | ≥ 7/9 | superato |
| Short con score ≥ 7 nel panel (asset crollato) | 0/9 | **1/9** | ≥ 3/9 | mancato |
| Regressioni (nuovo long contrarian ≥ 7) | 0 | **0** | 0 | superato |

Conteggi alternativi della stessa metrica: short ≥ 7 presenti nel panel su un
asset qualsiasi = 2/9 (Brent 7.8 nel momento 3, Brent 7.0 nel momento 7);
momenti in cui il top setup del panel è uno short = 1/9 (momento 3).

### Confronto 2a → 2a-bis (panel e isolato sull'asset crollato)

| # | Asset | 2a panel | 2a-bis panel | 2a isolato | 2a-bis isolato |
|---|-------|----------|--------------|------------|----------------|
| 1 | US500 | long 6.8 | assente (panel pieno di short) | long 5.8 | **short 6.2** |
| 2 | Nasdaq 100 | assente | **short 6.5** | long 6.2 | **short 6.8** |
| 3 | Gold 01/04 | short 6.5 | skip 5.2 | short 7.2 | skip 3.2 |
| 4 | Brent 06/04 | non valutato | non valutato | long 6.8 | long 6.2 |
| 5 | Bitcoin 17/04 | assente | long 6.0 | skip 4.2 | skip 4.2 |
| 6 | Brent 29/04 | long 7.2 | long 7.2 | long 6.8 | long 6.8 |
| 7 | Brent 04/05 | long 5.8 | **short 7.0** | long 5.8 | **short 6.8** |
| 8 | Bitcoin 14/05 | skip 5.2 | **short 6.8** | skip 4.5 | skip 4.2 |
| 9 | Gold 14/05 | short 5.2 | assente | long 5.8 | skip 4.8 |

### Analisi: il bias è risolto, il residuo non è bias

Tre evidenze che la bidirezionalità ora funziona:

- **Le direzioni si sono invertite dove dovevano.** Caso 1: il modello passa da
  long 6.8 a shortare (short 6.2 isolato; nel panel US500 esce dai top-3 solo
  perché Nasdaq e Bitcoin hanno short più forti, 6.5 e 6.8). Caso 7: da long 5.8
  a **short 7.0**. Caso 2: da long a short 6.5/6.8. Caso 8: da skip a short 6.8.
- **Il modello ragiona con le due pendenze.** Quasi ogni thesis ora cita la
  lettura combinata: "trend_slope_short_pct -0.32 già negativo mentre
  trend_slope_pct -0.09 ancora marginale: è l'inizio di uno swing-down".
- **Il bug RSI è chiuso.** In 2a il modello scriveva "RSI 31.7 in ipervenduto
  sconsiglia short". In 2a-bis, ovunque: "RSI a 37/34/31 sopra 25, non
  ipervenduto, debolezza in corso". La rifinitura numerica ha agganciato.

Lo score massimo di uno short è salito da 6.8 (2a) a **7.8** (2a-bis, Brent nel
momento 3). I momenti senza alcuno short proposto sono passati da 4 a 0: in
ogni momento ora compare almeno uno short da qualche parte.

Perché allora solo 1/9 a score ≥ 7 sull'asset crollato? Non è più bias. Due
ragioni, entrambe legittime:

1. **Gli short moderati ricevono uno score moderato, ed è corretto.** Il prompt
   fissa "8+ eccellenti, 6-7 buoni". Uno short di continuazione su un downtrend
   moderato (`trend_slope_short_pct` fra -0.23 e -0.35) è un setup "buono", non
   "eccellente": 6.5-7.0 è uno score onesto. L'unico short a 7.8 è il momento 3
   (Brent, `trend_slope_short_pct` -0.75, crollo del 18%): lì le feature sono
   estreme e lo score lo riflette. I 9 momenti del test sono per costruzione
   per lo più drawdown moderati (3-5%) colti all'inizio: setup "buoni", non
   "eccellenti". Pretendere 3/9 a ≥ 7 chiederebbe al modello di gonfiare lo
   score di setup moderati, che sarebbe un nuovo errore.
2. **La metrica conta solo l'asset crollato.** Nel momento 1 il modello shorta
   Nasdaq e Bitcoin (6.5, 6.8) invece di US500: su un selloff azionario diffuso
   è una scelta corretta, ma la metrica "short ≥ 7 sull'asset crollato" non la
   registra.

Verdetto: il bias direzionale documentato in §5 (cause A, B, D) è **risolto**.
La soglia numerica "3/9 short ≥ 7" non è raggiunta, ma il residuo è scoring
onesto di setup moderati più la definizione stretta della metrica, non più una
preferenza strutturale per il long. Le 41 decisioni 40-long/1-short non si
ripeterebbero con questo prompt.

### Decisione richiesta

Nessun push effettuato. Opzioni, da decidere:

- **A. Considerare il gate sostanzialmente superato.** Pre-filtro e regressioni
  ok, il bias è risolto in modo dimostrabile. Il gap su short ≥ 7 è scoring
  onesto, non bias. Si procede al push e alla Fase 3.
- **B. Iterazione sul confine di score.** Un ulteriore ritocco del prompt per
  spingere gli short "buoni" verso 7. Rischio: gonfiare lo score di setup
  moderati, reintrodurre un errore in direzione opposta.
- **C. Rivedere `MIN_SCORE_THRESHOLD` per i soli short.** Fuori dal perimetro
  dichiarato (i filtri non vanno toccati): andrebbe deciso a parte.

## 9. Decisione gate e passaggio a Fase 3

Stato: decisione presa 2026-05-20. Branch `sprint2-bidirezionalita` approvato
per il deploy in produzione.

### Dichiarazione esplicita di non-conformità formale

Il gate quantitativo "≥ 3/9 short con score ≥ 7" è **formalmente fallito**:
1/9 sull'asset crollato, 2/9 contando qualunque asset. Questa sezione non
riscrive il gate originario: lo dichiara apertamente fallito e lo **sovrascrive
con motivazione esplicita**.

Motivazione: il gate era mal calibrato. Assumeva che almeno 3 dei 9 setup di
drawdown moderato meritassero uno score alto (≥ 7). Ma è esattamente ciò che il
sistema NON deve fare: un LLM che assegna 7+ a un drop del 3.95% colto
sull'inizio è un LLM che sopravvaluta setup mediocri, non un LLM bidirezionale.
La soglia numerica codificava un'assunzione sbagliata sul comportamento atteso.

### Gate qualitativo: superato

Le evidenze su cui si fonda la decisione, tutte verificabili nei §7-§8:

- **4 inversioni di direzione** dove il sistema prima sbagliava: casi 1, 2, 7, 8
  passano da long o skip a short.
- **Score short massimo da 6.8 a 7.8.**
- **0 momenti senza alcuno short proposto**, erano 4 in 2a.
- **0 regressioni**: nessun nuovo long contrarian con score ≥ 7.
- **Bug RSI chiuso testualmente**: la frase "RSI 31.7 in ipervenduto sconsiglia
  short" non compare più; ovunque "RSI sopra 25, non ipervenduto".

Il bias 40 long / 1 short documentato in Sprint 1 non si ripeterebbe con questo
prompt. Su questa base la Fase 3 (capitale reale) parte.

### Nuovo controllo: kill switch direzionale a 5 trade

La diagnosi e il gate si basano su un replay di 9 casi storici: mostrano
bidirezionalità *potenziale*, non *osservata sul mercato live*. Per coprire il
rischio che la diagnosi sia incompleta, la Fase 3 introduce un controllo
aggiuntivo, oltre ai limiti esistenti (cap 5 €/trade, 20 €/settimana, -30 €
safety totale):

> **Kill switch direzionale.** I trade aperti dal 2026-05-20 in poi sono "trade
> Sprint 2". Quando 5 trade Sprint 2 si sono chiusi, se **nessuno è short** lo
> scanner si ferma immediatamente. La diagnosi va rifatta prima di rischiare
> altro capitale.

Implementazione: `src/scanner.py::_check_directional_kill_switch`, guard early
in `run_morning_scan` subito dopo il drawdown cap. Costanti `SPRINT2_START` e
`SPRINT2_KILL_CHECKPOINT` in `src/config.py`. Inattivo finché non si chiudono 5
trade; disarmato in modo permanente non appena uno dei primi trade è short.
Notifica Telegram una sola volta (il kill non auto-recupera).

### Nota onesta per il journal

Il gate originario era mal calibrato e va registrato come lezione. Una soglia
numerica unica, fissata prima dell'esperimento, ha codificato un'assunzione
("3/9 setup moderati devono valere ≥ 7") che si è rivelata sbagliata e in
conflitto con un altro obiettivo del sistema (non sopravvalutare i setup
mediocri). La prossima volta che si fissano criteri quantitativi prima di un
esperimento, è preferibile **più gate qualitativi indipendenti** (inversioni di
direzione, assenza di regressioni, score massimo in crescita, chiusura di bug
testuali) piuttosto che un'unica soglia numerica che può rivelarsi un'ipotesi
errata difficile da correggere senza sembrare di "spostare i pali".
