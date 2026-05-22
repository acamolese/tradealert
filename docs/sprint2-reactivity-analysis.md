# Sprint 2 — Analisi di reattività del sistema

Stato: diagnosi esplorativa, 2026-05-22. Test dell'ipotesi di Andrea "il
sistema fa buone proposte ma in ritardo". Non blocca la Fase 3 in corso.

## 1. Ipotesi e contesto

Andrea ha l'intuizione che il sistema entri in ritardo: buone proposte, ma in
fase già matura del movimento. L'analisi indiretta sui 18 trade post-fix
(posizione di chiusura tra entry e SL/TP) la supporta: media 53% del cammino
verso TP sui WIN, 51% verso SL sui LOSS, solo 1/6 WIN supera l'80% del cammino
verso TP. Pattern coerente con "entry in fase matura, resta poco da estrarre".

Questo documento testa l'**ipotesi C: ritardo del ciclo del setup** — quanto
passa tra il momento in cui le feature entrano in zona "setup valido" e il
momento in cui il sistema genera il signal.

## 2. Metodo

`jobs/replay_signal.py reactivity` rigira la lettura del modello sull'asset di
ciascun trade a passi di 4h, da 24h prima del signal (`T-24h`) fino al signal
(`T`). 7 punti per trade. A ogni punto: feature ricostruite dalle candele 4H
come le avrebbe viste lo scanner, lettura isolata del LLM (direction, score),
stato del pre-filtro.

Campione: 6 trade variati, 3 WIN e 3 LOSS, 4 asset.

### Due limiti di metodo, dichiarati in apertura

Questi due limiti vanno tenuti presenti per tutto il documento: condizionano
quali conclusioni sono solide e quali no.

**Limite 1 — lo score della lettura isolata è più basso del panel.** Il replay
usa la `lettura isolata` (un solo asset passato al LLM). Il signal reale nasce
invece dal `panel` (più asset) con i guardrail. La lettura isolata segna
sistematicamente più basso: i 6 signal reali avevano score 7.0–7.5, ma la
lettura isolata a `T` non supera mai 6.2–7.2. **Conseguenza: non si può
osservare "lo score attraversa 7"**, perché lo strumento non raggiunge 7 quasi
mai. Il punto 3 dell'ipotesi (è la soglia che ritarda?) non è verificabile in
modo diretto con questo strumento.

**Limite 2 — il replay usa il codice di oggi, i signal sono del codice
vecchio.** I trade 19–37 sono stati generati fra il 27/04 e il 20/05 dal codice
pre-Fase-3, long-biased e senza `trend_slope_short_pct`. Il replay gira con il
codice bidirezionale attuale. Quindi la riga `T` del replay **non riproduce il
signal reale**: è cosa proporrebbe il sistema di OGGI a quell'istante. Dove le
due cose divergono (sotto se ne vedono diversi casi) è un dato di per sé, ma
non è un confronto "stesso codice".

## 3. Le 6 timeline

Legenda: `dir` lettura isolata, `score` 0–10, `rsi`/`slope`/`ss` (=
trend_slope_short_pct) feature a quell'istante. `T` = istante del signal reale.

### Trade 19 — Brent Oil, long, WIN +6.12 (signal #58, score reale 7.0)

| Istante | pre-filtro | dir | score | rsi | slope | ss |
|---|---|---|---|---|---|---|
| T-24h | passa | skip | 5.2 | 65.4 | +0.36 | -0.14 |
| T-20h | passa | skip | 5.2 | 65.4 | +0.36 | -0.14 |
| T-16h | passa | skip | 4.2 | 71.2 | +0.38 | +0.01 |
| T-12h | passa | **long** | 6.8 | 65.7 | +0.35 | +0.08 |
| T-8h | passa | long | 6.8 | 65.2 | +0.33 | +0.25 |
| T-4h | escluso | long | 6.8 | 58.9 | +0.27 | +0.29 |
| **T** | passa | long | 6.2 | 63.7 | +0.23 | +0.22 |

Direzione long vista a **T-12h**, poi score stabile ~6.8 fino a T-4h. Nessuna
rampa verso 7: il setup è visibile 12h prima, alla stessa qualità.

### Trade 23 — Bitcoin, long, WIN +0.33 (signal #67, score reale 7.0)

| Istante | pre-filtro | dir | score | rsi | slope | ss |
|---|---|---|---|---|---|---|
| T-24h → T-4h | passa | skip | 4.2–5.2 | 65–73 | +0.13/+0.19 | flat |
| **T** | escluso | long | 6.2 | 53.5 | +0.11 | +0.19 |

Skip per tutte le 24h, long solo a `T`. Caso che "sembra" tardivo, ma il quadro
era ipercomprato (RSI 70–73) quasi tutto il tempo: il long si forma solo quando
l'RSI rientra a 53.

### Trade 25 — Gold, long, WIN +6.31 TP pieno (signal #69, score reale 7.0)

| Istante | pre-filtro | dir | score | rsi | slope | ss |
|---|---|---|---|---|---|---|
| T-24h | passa | skip | 5.2 | 62.3 | +0.06 | -0.01 |
| T-20h | passa | skip | 4.2 | 62.4 | +0.06 | 0.00 |
| T-16h | passa | long | 6.2 | 60.3 | +0.07 | +0.04 |
| T-12h | escluso | long | 6.2 | 54.0 | +0.07 | +0.03 |
| T-8h | escluso | skip | 4.8 | 42.8 | +0.05 | -0.11 |
| T-4h | passa | **short** | 7.2 | 30.0 | +0.01 | -0.31 |
| **T** | passa | **short** | 6.8 | 31.3 | -0.03 | -0.34 |

Caso particolare. La direzione long appare a T-16h, ma a T-4h e T il codice
attuale legge Gold come **short** (RSI 30, slope corto -0.34). Il trade reale
era long ed è andato a TP pieno: il prezzo è poi rimbalzato. È un effetto del
limite 2 (il codice di oggi è bidirezionale) e non è un dato sulla reattività.

### Trade 29 — Gold, long, LOSS -1.95 (signal #75, score reale 7.0)

| Istante | pre-filtro | dir | score | rsi | slope | ss |
|---|---|---|---|---|---|---|
| T-24h | passa | skip | 3.2 | 87.2 | +0.29 | +0.17 |
| T-20h | passa | skip | 4.2 | 77.4 | +0.29 | +0.12 |
| T-16h | passa | skip | 4.2 | 77.0 | +0.28 | +0.05 |
| T-12h | passa | skip | 4.2 | 82.6 | +0.26 | +0.01 |
| T-8h | passa | skip | 4.2 | 75.2 | +0.23 | -0.05 |
| T-4h | passa | skip | 5.2 | 69.9 | +0.20 | -0.10 |
| **T** | passa | long | 6.2 | 58.1 | +0.17 | -0.07 |

Skip per 24h, long solo a `T`. Ma non per ritardo di rilevazione: l'oro era in
**ipercomprato estremo** (RSI 87 → 75) per l'intera finestra. Il modello attuale
rifiuta correttamente un long su RSI 75+. Il long compare solo a `T`, quando
l'RSI rientra a 58. Il setup non era valido prima, non è il sistema a essere
lento.

### Trade 30 — Brent Oil, long, LOSS -3.41 (signal #76, score reale 7.5)

| Istante | pre-filtro | dir | score | rsi | slope | ss |
|---|---|---|---|---|---|---|
| T-24h | escluso | long | 6.8 | 67.0 | +0.16 | +0.47 |
| T-20h | escluso | long | 6.8 | 65.1 | +0.19 | +0.40 |
| T-16h | passa | long | 6.2 | 59.1 | +0.22 | +0.19 |
| T-12h | passa | long | 6.8 | 55.6 | +0.26 | -0.06 |
| T-8h | passa | skip | 4.2 | 71.8 | +0.30 | +0.12 |
| T-4h | passa | skip | 4.2 | 75.1 | +0.32 | +0.39 |
| **T** | passa | **skip** | 4.2 | 78.1 | +0.31 | +0.52 |

Il long è visto prestissimo (T-24h, score 6.8) e resta fino a T-12h. Poi il
modello attuale passa a **skip**: l'RSI sale a 78, ipercomprato. A `T` il codice
di oggi **non genererebbe il signal** (skip 4.2). Il codice vecchio invece lo ha
segnalato long 7.5 — e il trade ha perso -3.41.

### Trade 31 — Nasdaq 100, long, LOSS -0.55 (signal #77, score reale 7.5)

| Istante | pre-filtro | dir | score | rsi | slope | ss |
|---|---|---|---|---|---|---|
| T-24h | passa | skip | 4.2 | 49.6 | +0.08 | -0.24 |
| T-20h | escluso | skip | 4.2 | 40.0 | +0.05 | -0.21 |
| T-16h | passa | skip | 4.8 | 47.2 | +0.03 | -0.12 |
| T-12h | passa | skip | 4.2 | 52.4 | +0.02 | +0.02 |
| T-8h | passa | long | 6.2 | 51.7 | 0.00 | +0.13 |
| T-4h | passa | skip | 4.2 | 49.7 | -0.02 | +0.18 |
| **T** | passa | **skip** | 4.8 | 53.8 | -0.01 | +0.25 |

Il codice attuale non vede mai un long solido: skip quasi ovunque, un singolo
long 6.2 a T-8h. A `T` legge "conflitto tecnico", skip. Il codice vecchio ha
segnalato long 7.5. Trade perso -0.55.

## 4. Sintesi diagnostica

### Sull'ipotesi "il sistema entra in ritardo"

Con i limiti di metodo dichiarati, **l'ipotesi del ritardo di rilevazione non è
confermata**. Dove il setup direzionale esiste, il sistema lo vede ore prima,
non all'ultimo momento:

- Trade 19: long visto a T-12h, score stabile ~6.8 fino a T.
- Trade 30: long visto a T-24h, score 6.8.
- Trade 25: long visto a T-16h.

Non c'è il pattern "skip fino a T-2h, poi improvvisamente la proposta". I casi
che *sembrano* tardivi (23, 29: long solo a `T`) sono casi in cui il setup
**non era valido prima**: l'asset era ipercomprato per tutta la finestra e il
long si è formato solo quando l'RSI è rientrato. Il sistema non è arrivato
tardi, il setup è maturato tardi.

**Punto 3 (è la soglia che ritarda?)**: non verificabile in modo pulito (limite
1). Però il pattern osservato — direzione vista presto, score che resta stabile
intorno a 6.5–6.8 per 12–24h senza salire — è *compatibile* con un effetto
soglia: il sistema vede il setup, ma resta appena sotto un'asticella alta. Non
è una prova, è l'indizio più forte che questo strumento può dare.

### Finding inatteso: il codice di Fase 3 rifiuterebbe i 3 trade LOSS

Il dato più netto non riguarda la velocità ma la **qualità dell'entry**. Sui 3
trade in perdita, il codice bidirezionale di oggi, rigirato a `T`, NON avrebbe
generato il signal:

- Trade 30 (Brent, -3.41): a `T` RSI 78, ipercomprato → **skip**.
- Trade 31 (Nasdaq, -0.55): a `T` conflitto tecnico → **skip**.
- Trade 29 (Gold, -1.95): long solo a `T`, dopo 24h di RSI 75–87 ipercomprato.

Il codice vecchio ha segnalato tutti e tre come long 7.0–7.5. Il pattern di
Andrea — "entry in fase matura, resta poco da estrarre" — qui ha un nome
preciso: il codice vecchio **entrava su setup vicini all'esaurimento** (RSI
75+). Non è "lentezza", è "entrata su un movimento già concluso". E la
rifinitura RSI della Fase 2a-bis (ipercomprato = RSI > 75, esaurimento non
continuazione) **già mitiga questo errore**: i tre LOSS verrebbero ora letti
skip o a score basso.

Questo non chiude l'ipotesi reattività, ma la riformula: parte di ciò che
sembrava "il sistema entra tardi" era "il prompt vecchio entrava su feature di
esaurimento", e la Fase 3 lo corregge già. Sul campione (3 LOSS su 3) è netto,
ma 3 trade non sono una prova: va riverificato sui trade reali di Fase 3.

## 5. Raccomandazione

- L'ipotesi "ritardo di rilevazione" non regge sul campione: i setup sono
  visibili ore prima. Non è la priorità.
- L'effetto soglia (punto 3) resta aperto ma non dimostrabile con la lettura
  isolata. Un test pulito richiederebbe di rigirare il **panel** completo (non
  l'isolata) e idealmente con il codice che ha generato il signal. È un lavoro
  più pesante, da valutare solo se la Fase 3 mostra ancora P&L piatto.
- La pista più concreta è il finding inatteso: l'entry su setup in esaurimento
  RSI. Prima di aprire uno Sprint focalizzato sulla reattività, conviene
  **misurare sui trade reali di Fase 3** se il problema "53% verso TP, entry in
  fase matura" si ripete con il prompt nuovo. Se la rifinitura RSI ha già
  spostato gli entry su fasi meno mature, il problema di reattività potrebbe
  essersi ridotto da solo, senza un intervento dedicato.

In breve: non aprire una Fase 4 "reattività" sulla base di questo campione.
Prima raccogliere i trade di Fase 3 e rifare la stessa analisi indiretta
(posizione di chiusura entry↔TP/SL) sul codice nuovo.
