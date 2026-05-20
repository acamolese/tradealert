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

| # | T (UTC) | Asset | Drop | Feature a T | Pre-filtro | LLM panel (asset) | LLM isolato | Classe |
|---|---------|-------|------|-------------|-----------|-------------------|-------------|--------|
| 1 | 2026-03-26 16:00 | US500 | 3.95% | rsi 31.7, slope -0.04, daily -1.6, pfh -2.17 | **ESCLUSO** | non valutato | long 5.5 | PROMPT |
| 2 | 2026-03-26 16:00 | Nasdaq 100 | 4.92% | rsi 28.0, slope -0.07, daily -2.18, pfh -2.86 | passa | long 5.5 | long 6.5 | PROMPT |
| 3 | 2026-04-01 04:00 | Gold | 4.81% | rsi 84.3, slope +0.28, daily +0.36, pfh 0.0 | passa | skip 4.0 | skip 4.5 | PROMPT |
| 4 | 2026-04-06 00:00 | Brent Oil | 18.42% | rsi 59.4, slope +0.14, daily -1.66, pfh -1.67 | **ESCLUSO** | non valutato | long 5.5 | FEATURE |
| 5 | 2026-04-17 04:00 | Bitcoin | 4.36% | rsi 58.5, slope +0.04, daily -0.1, pfh -0.37 | passa | skip 4.5 | long 5.5 | FEATURE |
| 6 | 2026-04-29 04:00 | Brent Oil | 6.73% | rsi 66.3, slope +0.30, daily +1.97, pfh 0.0 | passa | **long 7.5** | long 7.0 | FEATURE |
| 7 | 2026-05-04 04:00 | Brent Oil | 15.62% | rsi 37.0, slope -0.09, daily +1.57, pfh -3.35 | **ESCLUSO** | non valutato | long 5.5 | PROMPT |
| 8 | 2026-05-14 04:00 | Bitcoin | 5.33% | rsi 36.7, slope -0.14, daily +0.53, pfh -2.73 | passa | non in top 3 | skip 4.5 | PROMPT |
| 9 | 2026-05-14 16:00 | Gold | 4.36% | rsi 38.3, slope -0.07, daily -0.9, pfh -2.37 | **ESCLUSO** | non valutato | skip 4.5 | FILTRO |

Dato trasversale: **in nessuno dei 9 momenti, su 36 output di proposal
complessivi, uno short ha mai ricevuto score ≥ 7**. Lo short con score più alto
è 6.5 (Brent, panel del caso 3). I long hanno toccato 7.5 ripetutamente. La
lettura isolata sull'asset crollato ha prodotto 6 long, 3 skip, **0 short**.

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
- **Classe: PROMPT.** Le feature mostrano debolezza (RSI vicino a ipervenduto,
  daily negativo, prezzo sotto i massimi); il modello la converte in tesi long.
  La causa primaria del mancato short è l'interpretazione, non l'assenza di
  segnale. L'esclusione dal pre-filtro è un problema secondario di copertura
  (vedi §5-D).

### Caso 2 — Nasdaq 100, 2026-03-26, drop 4.92%

- **Cosa ha proposto il LLM**: panel → **Nasdaq long, score 5.5**; lettura
  isolata → **long, score 6.5**, thesis "RSI 4H a 28, zona di ipervenduto
  tecnico che storicamente anticipa rimbalzi di breve".
- **Short proposto?** No.
- **Long contrarian?** Sì, esplicito: `rsi 28` è il segnale tecnico più chiaro
  di tutto il caso, e il modello lo usa come argomento **a favore di un long**.
- **Classe: PROMPT.** Il feature bearish c'è ed è inequivocabile (RSI 28). Il
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
- **Classe: PROMPT.** `rsi 84` è un segnale di esaurimento da manuale, il
  candidato short più pulito dei 9. Il prompt insegna al modello a leggerlo solo
  come "non comprare", mai come "vendi": lo short è ammesso solo come
  continuazione di un trend già negativo. Al top di un movimento il sistema è
  strutturalmente incapace di shortare.

### Caso 4 — Brent Oil, 2026-04-06, drop 18.42%

- **Cosa ha proposto il LLM**: panel → Brent escluso dal pre-filtro; top setup
  Nasdaq long 6.5. Lettura isolata Brent → **long, score 5.5**.
- **Short proposto?** No.
- **Classe: FEATURE.** A T le feature mostrano un uptrend: `slope +0.14`
  positivo, `rsi 59.4` in zona rialzista, prezzo a -1.67% dai massimi. Il crollo
  del 18% non è ancora visibile in nessun numero tecnico. Anche un analista
  onesto, guardando solo quei dati, non shorterebbe. Il problema qui è che
  l'istante T cattura l'inizio del movimento prima che la struttura 4H si
  inverta.

### Caso 5 — Bitcoin, 2026-04-17, drop 4.36%

- **Cosa ha proposto il LLM**: panel → BTC skip 4.5; lettura isolata → long 5.5
  debole.
- **Short proposto?** No.
- **Classe: FEATURE.** Quadro completamente neutro: `slope +0.04` piatto, `rsi
  58.5` neutro, `pfh -0.37` (sui massimi), `daily -0.1` nullo. Non esiste nessun
  segnale, né long né short. Il modello stesso assegna 5.5 e cita "assenza di
  trigger". I numeri tecnici non mostrano un pattern short perché a T non c'è
  ancora nulla.

### Caso 6 — Brent Oil, 2026-04-29, drop 6.73%

- **Cosa ha proposto il LLM**: panel → **Brent long, score 7.5** (sopra la
  soglia di 7: a questo istante il sistema avrebbe sparato un signal long).
  Lettura isolata → **long, score 7.0**.
- **Short proposto?** No.
- **Classe: FEATURE.** A T le feature gridano long: `slope +0.30` (il più forte
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
- **Classe: PROMPT.** A T ci sono segnali di debolezza (`slope -0.09` negativo,
  `rsi 37` in zona bassa). Il modello li trasforma in una tesi di rimbalzo long
  invece che di continuazione short. Stesso meccanismo dei casi 1-2.

### Caso 8 — Bitcoin, 2026-05-14, drop 5.33%

- **Cosa ha proposto il LLM**: panel → BTC non rientra nei top 3 (3 proposal,
  tutti long su Brent/US500/Nasdaq). Lettura isolata → **skip, score 4.5**,
  thesis "trend_slope negativo debole... RSI in debolezza... non offre asimmetria
  sufficiente".
- **Short proposto?** No: il modello non formula mai una direzione short, va
  direttamente a skip.
- **Classe: PROMPT.** Qui le feature sono chiaramente bearish (`slope -0.14`
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
- **Classe: FILTRO.** È l'unico caso in cui il LLM vede lo short, lo nomina, e
  lo elimina per RR/confidenza insufficiente. Da notare: non è un filtro di
  codice (il `min_rr_at_entry` agisce solo al click, in `_handle_confirm`), è il
  gate "asimmetria 2:1 + selettività" del system prompt che il modello applica
  in modo più severo agli short che ai long.

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

Classificazione finale: **PROMPT 5/9, FEATURE 3/9, FILTRO 1/9.**

Il bias è **prevalentemente nel prompt**. Le tre classi però non sono
indipendenti: si sommano in un imbuto che rende lo short quasi impossibile.

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

### D. Il pre-filtro non intercetta i ribassi moderati

`_prefilter_candidates` fa passare un asset solo se `|daily_pct| ≥ 2` oppure
`rsi ≤ 30 / ≥ 70` oppure `|pfh| ≤ 1` o `pfh ≤ -8` oppure `bb_width ≤ 1.5`. Un
calo giornaliero dell'1,5-2% con RSI intorno a 32-37 (l'inizio tipico di uno
swing-down) **non soddisfa nessuna soglia**. In 4 casi su 9 (1, 4, 7, 9) l'asset
crollato è stato escluso dal pre-filtro e il LLM non l'ha mai visto. Non è un
bias long in sé (il pre-filtro è simmetrico), ma è una falla di copertura: i
ribassi moderati sono invisibili a monte, prima ancora che si parli di prompt.

### E. Lo score degli short è strutturalmente sotto soglia

Anche quando lo short emerge, il LLM gli assegna 5.5-6.5, mai ≥ 7, e lo motiva
con l'asimmetria sfavorevole (caso 1) o l'assenza di catalyst/confidenza (caso
9). Con `MIN_SCORE_THRESHOLD = 7` nessuno short può diventare signal. Questo è
prompt (B) più la selettività "2:1 + sii selettivo" applicata più severamente
agli short che ai long.

### Conclusione

I 40 long / 1 short si spiegano integralmente: il pre-filtro non fa nemmeno
arrivare al modello i ribassi moderati (D); per quelli che arrivano, il prompt
non offre un template di short e spinge il modello a leggere ogni debolezza come
occasione di rimbalzo long o come skip (A, B); la feature di trend è troppo
lenta per mostrare un pattern short all'inizio del movimento (C); e quando uno
short sopravvive comunque, lo score che il modello gli dà è sotto soglia (E).

**La leva con il rapporto impatto/sforzo più alto è il prompt (A + B + E).** È
la causa di 5 casi su 9 diretti, contribuisce a E, ed è l'unica delle quattro
modificabile senza toccare il calcolo delle feature o le soglie. FEATURE (C) e
copertura del pre-filtro (D) sono cause reali ma di secondo ordine e vanno
affrontate dopo aver validato l'effetto della revisione del prompt.

## 6. Perimetro e Fase 2

Questa fase è solo diagnosi: **nessuna modifica al sistema è stata applicata**.
Lo script `jobs/replay_signal.py` è committato e riutilizzabile per misurare
l'effetto di qualunque intervento (rigirare gli stessi 9 momenti dopo una
modifica e confrontare gli output).

Ipotesi da validare in Fase 2, in ordine di priorità suggerito, da decidere
dopo lettura di questo documento:
1. Riequilibrare il system prompt: template esplicito di short, esempio JSON
   short, `key_factors` short, lettura simmetrica dell'RSI.
2. Verificare se lo score minimo o la regola "2:1 + selettività" vanno
   ricalibrati per non penalizzare gli short.
3. Valutare una feature di trend a finestra più corta (o un secondo slope
   veloce) per ridurre il ritardo dei casi FEATURE.
4. Estendere i criteri del pre-filtro per intercettare i ribassi moderati.
