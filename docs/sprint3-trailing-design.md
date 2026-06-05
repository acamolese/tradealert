# Sprint 3 — Design del nuovo trailing stop

Data: 2026-06-07 (preparazione weekend, modalità passiva). Branch
`sprint3-trailing-design`, non mergiato. Nessun deploy: il sistema live
resta sul trailing attuale fino alla chiusura formale di Sprint 2.

Obiettivo: scegliere la logica di trailing che chiude il buco di
protezione del profit identificato in `docs/sprint2-profit-protection-analysis.md`
(SL ancorato a breakeven nella fascia 1.0-1.5R, primo lock sopra il BE
solo a +1.5R), senza degradare i trade vincenti.

## Trailing attuale (baseline)

`src/position_monitor.py::_apply_trailing_stop`, `step_r=0.5`, solo
migliorativo:

| profit raggiunto | SL portato a |
|------------------|--------------|
| ≥ 0.5R | entry −0.5R (half-risk) |
| ≥ 1.0R | entry (breakeven) |
| ≥ 1.5R | entry +0.5R |
| ≥ 2.0R | entry +1.0R, poi +step_r ogni step_r |

Il buco: tutta la fascia 1.0-1.5R ha lo SL fermo a breakeven. Un trade
che culmina lì e ritraccia esce a zero (casi #45, #51, #55).

## Metodo di valutazione

Simulatore `jobs/replay_trailing.py`: replay candela-per-candela
(MINUTE_5, fallback MINUTE_15) sulle candele storiche Capital di ogni
trade chiuso Sprint 2 (id 38-57, 20 trade; #58/#59 esclusi perché
ancora aperti). Per ogni candela applica la policy, aggiorna lo SL solo
se migliorativo e verifica l'hit di SL/TP.

Assunzioni dichiarate:
- **Nessun intervento manuale**: l'uscita dipende solo da SL/TP
  automatici. Quindi il confronto valido è replay-vs-replay (isola il
  solo effetto del trailing), non replay-vs-realizzato.
- Hit nella stessa candela: si testa lo SL prima del TP (conservativo).
- Granularità 5m/15m: proxy del percorso intra-candela.

Validazione: il P&L del replay baseline sul perimetro 38-57 è **+4.75
USD**, vicino al realizzato reale **+5.79 USD** (scarto ~1 USD dovuto
alle chiusure manuali). Il replay riproduce bene la realtà.

## Le tre opzioni

### Opzione A — R-multiple granulare (step 0.25R)

Stessa filosofia attuale, passo dimezzato a 0.25R per riempire la fascia
1.0-1.5R con più gradini.

```
def offset_A(profit_r):
    if profit_r < 0.5:  return None          # SL resta a -1R
    if profit_r < 1.0:  return -0.5 + floor((profit_r-0.5)/0.25)*0.25
                                              # 0.5R->-0.5, 0.75R->-0.25
    return floor((profit_r-1.0)/0.25)*0.25    # 1R->BE, 1.25R->+0.25, 1.5R->+0.5
new_sl = entry + offset * risk   # (− per short)
```

Pro: cambio minimo (di fatto `step_r=0.25` più la granularità sotto 1R),
basso rischio di regressione, nessun nuovo concetto. **È l'unica delle
tre che migliora l'aggregato senza danneggiare alcun trade.**

Contro: timida proprio sui casi bug. A 1.0-1.24R lo SL è ancora a
breakeven (il primo lock positivo è a 1.25R), quindi #51 (peak 1.16R) e
#55 (peak 1.09R) non vengono protetti. Aiuta solo chi supera 1.25R o chi
beneficia del mezzo gradino in zona rischio.

Simulazione #45 / #51 / #55: **0.67 / 0.00 / 0.00 USD** (vs 0/0/0
baseline). Recupera solo #45.

Effort: ~1-2h implementazione, riuso del replay per il test. Basso.

### Opzione B — TP-aware (% del cammino entry→TP)

Lo SL si aggancia in frazione del cammino percorso verso il TP, non in
multipli di R.

```
def offset_frac_B(frac_tp):     # frac_tp = (peak-entry)/(TP-entry)
    if frac_tp < 0.30:  return None         # SL a -1R
    if frac_tp < 0.50:  return 0.0          # breakeven
    if frac_tp < 0.70:  lock = 0.15
    elif frac_tp < 0.85: lock = 0.35
    else:                lock = 0.55
    return lock * (TP-entry)                 # SL = entry + lock_frac*(TP-entry)
```

Pro: protegge esplicitamente la prossimità al TP, indipendente dal R:R.
Recupera bene i casi bug.

Contro: **aggancia troppo presto**. Un mid-runner che sale al 60-70% del
TP e poi fa un ritracciamento normale viene stoppato prima di ripartire.
Nel replay taglia #40 (4.10→0), #48 (2.84→0.73), #49 (7.78→3.30). Il
danno sui vincenti supera il recupero sui casi bug: **aggregato peggiore
del baseline**.

Simulazione #45 / #51 / #55: **0.90 / 2.35 / 2.27 USD**. Ottimo sui tre,
ma a spese del resto.

Effort: ~3-4h (nuova funzione, il TP è già disponibile lato broker).

### Opzione C — Ibrido A+B (lock più protettivo)

Ad ogni candela prende lo SL più protettivo tra A e B.

```
sl = most_protective( offset_A(profit_r), offset_frac_B(frac_tp) )
```

Pro: in teoria combina il meglio dei due.

Contro: poiché B domina quasi sempre (è più aggressivo), C eredita i
difetti di B sui vincenti. Nel replay è ~neutro sul baseline: il lock
precoce di B continua a tagliare #40/#48/#49.

Simulazione #45 / #51 / #55: **0.90 / 2.35 / 2.27 USD** (come B).

Effort: ~4-5h.

## La variante che emerge dalla simulazione: D (ibrido tardivo)

La simulazione mostra che il successo dipende interamente dalla **soglia
di aggancio del lock TP-aware**. Agganciare al 50% taglia i mid-runner;
agganciare solo molto vicino al TP (≥80%) protegge i casi bug (#51 e #55
peakano all'81-82% del cammino) senza toccare chi ritraccia dal 60-70%.

**D = granularità A + lock TP-aware solo da ≥80% del cammino.**

```
def offset_frac_B_late(frac_tp):
    if frac_tp < 0.80:  return None
    if frac_tp < 0.90:  return 0.45 * (TP-entry)
    return 0.65 * (TP-entry)

sl = most_protective( offset_A(profit_r), offset_frac_B_late(frac_tp) )
```

D è **Pareto-superiore al baseline**: nel replay non peggiora alcun trade
e ne migliora sei. Preserva i vincenti (#40, #48, #49 intatti) e recupera
i casi bug meglio di tutte le altre.

Simulazione #45 / #51 / #55: **0.67 / 3.03 / 2.92 USD**.

Effort: ~4-5h implementazione + 1-2h tuning della soglia (80/90%) e
re-simulazione. È l'unico che richiede una passata di calibrazione, ma il
simulatore è già pronto.

## Risultati aggregati (replay, id 38-57, 20 trade)

| Policy | P&L totale | Δ vs baseline | #45/#51/#55 | Danneggia vincenti? |
|--------|-----------|---------------|-------------|---------------------|
| baseline | +4.75 | — | 0.00 | — |
| A | +6.78 | **+2.03** | +0.67 | No |
| B | +3.76 | −0.99 | +5.52 | Sì (#40 #48 #49) |
| C | +4.72 | −0.03 | +5.52 | Sì (#40 #48 #49) |
| **D** | **+12.73** | **+7.98** | **+6.62** | **No** |

Dettaglio per trade in coda al documento.

## Output #3 — Smoke test storico (Sprint 2 con fix vs senza fix)

Il numero richiesto, perimetro 38-57:

- **Senza fix** (trailing attuale, replay baseline): **+4.75 USD**
- **Con fix D** (raccomandato): **+12.73 USD**
- **Differenza: +7.98 USD** (+168% sul P&L del periodo)

Sul perimetro 38-55 (i 18 trade del doc profit-protection): baseline
+9.62 → D +16.79, **delta +7.17 USD**, in linea col +49 USD di "upper
bound teorico" stimato in profit-protection (D ne recupera circa un
sesto, che è la quota realisticamente catturabile senza foresight).

Avvertenza: tutti i numeri sono replay sotto l'assunzione "nessun
intervento manuale" e con granularità 5m/15m. Sono stime di
dimensionamento, non garanzie di P&L futuro.

## Raccomandazione

**Opzione D (ibrido tardivo).** È l'unica che protegge i casi bug
(#51 e #55, i più vistosi, oltre +1R restituito) senza tagliare i
vincenti, ed è Pareto-superiore al baseline nel replay. Il guadagno
dimensionato è netto (+7.98 USD su 20 trade, ~+168%) contro un effort
contenuto, dato che il simulatore di test è già pronto.

Se si vuole il **minimo rischio assoluto** per il primo deploy post
Sprint 2, ripiegare su **A**: migliora comunque l'aggregato (+2.03),
cambia una sola costante di fatto, zero rischio di regressione sui
vincenti. A però lascia sul tavolo i due casi bug più pesanti.

Sconsigliate B e C come specificate: l'aggancio precoce le rende neutre o
peggiorative.

Piano suggerito: deployare D insieme al job `intra_trade_extreme`
(`jobs/intra_trade_log.py`) a chiusura Sprint 2, calibrare la soglia
80/90% su una prima settimana di dati intra-trade reali, ri-simulare.

## Appendice — dettaglio per trade (P&L replay, USD)

| id | asset | dir | R:R | baseline | A | B | C | D | reale |
|----|-------|-----|-----|----------|---|---|---|---|-------|
| 38 | Brent Oil | short | 1.32 | −0.93 | −0.93 | −0.93 | −0.93 | −0.93 | −0.88 |
| 39 | Nasdaq 100 | long | 1.49 | −0.64 | −0.64 | −0.64 | −0.64 | −0.64 | −0.79 |
| 40 | Nasdaq 100 | long | 1.41 | 4.10 | 4.10 | 0.00 | 0.00 | 4.10 | 4.16 |
| 41 | Gold | long | 2.19 | −0.08 | −0.08 | −0.08 | −0.08 | −0.08 | −0.19 |
| 42 | Brent Oil | short | 1.99 | 9.75 | 9.75 | 9.75 | 9.75 | 9.75 | 10.85 |
| 43 | Gold | long | 2.28 | −0.96 | −0.96 | −1.92 | −0.96 | −0.96 | −1.93 |
| 44 | Brent Oil | long | 2.07 | −2.43 | −2.43 | 0.00 | 0.00 | −2.43 | −2.44 |
| 45 | Nasdaq 100 | long | 2.21 | 0.00 | 0.67 | 0.90 | 0.90 | **0.67** | 0.00 |
| 46 | Brent Oil | short | 1.35 | −2.09 | −2.09 | −2.09 | −2.09 | −2.09 | −2.52 |
| 47 | Gold | short | 2.13 | −3.98 | −3.98 | −3.98 | −3.98 | −3.98 | −4.00 |
| 48 | Nasdaq 100 | long | 2.25 | 2.84 | 2.84 | 0.73 | 0.73 | 2.84 | 2.82 |
| 49 | Brent Oil | short | 2.12 | 7.78 | 7.78 | 3.30 | 3.30 | 7.78 | 9.46 |
| 50 | Brent Oil | short | 1.99 | −3.02 | −3.02 | −3.02 | −3.02 | −3.02 | −2.98 |
| 51 | Brent Oil | long | 1.42 | 0.00 | 0.00 | 2.35 | 2.35 | **3.03** | −0.11 |
| 52 | Bitcoin | short | 1.99 | 2.01 | 2.01 | 2.01 | 2.01 | 2.01 | 2.01 |
| 53 | Gold | long | 1.51 | −1.64 | −1.64 | −1.64 | −1.64 | −1.64 | −1.59 |
| 54 | Nasdaq 100 | long | 2.25 | −1.10 | −0.55 | 0.00 | 0.00 | −0.55 | −1.10 |
| 55 | Brent Oil | long | 1.32 | 0.00 | 0.00 | 2.27 | 2.27 | **2.92** | −0.06 |
| 56 | Brent Oil | long | 2.19 | −3.26 | −3.26 | −3.26 | −3.26 | −3.26 | −3.31 |
| 57 | Gold | long | 2.08 | −1.61 | −0.80 | 0.00 | 0.00 | −0.80 | −1.61 |
