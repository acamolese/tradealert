# Sprint 4 — Trailing minimal fix: anticipo del lock nella fascia 0.5-1.0R

Data: 2026-06-10. Analisi offline + sim rumore-aware + pre-registrazione forward.
Sola lettura (candele Capital + DB anon), monitor invariato, **nessun deploy in
questo giro**. Gate pre-registrato prima degli esiti. Script:
`jobs/trailing_minimal_fix.py`. Sample: 27 trade chiusi 38-64.

**Sintesi in una riga**: la minimal fix come specificata **non regge**. Il
raccordo a 1.0-1.25R taglia i trend nascenti (−1.77R) più di quanto la
protezione della fascia bassa renda (+1.50R), quindi è peggio di D già in
modalità base (−0.27R) e peggio ancora rumore-aware (−0.68R). Verdetto: **no
deploy**. Sorpresa collaterale: il gap 0.25 sopravvive al rumore (resta sopra D),
e il "buco fascia bassa" è reale ma va tappato con una variante che non tocchi la
fascia 1.0-1.25R.

## Task 1 — Spec della minimal fix (pre-registrata)

```
offset_minimal(peak):
  peak < 0.5        -> -1.0                  (no trailing, come D)
  0.5 <= peak <= 1.0 -> granular_D(peak)+0.25 (anticipo di un gradino)
  1.0 <  peak < 1.25 -> +0.25                 (raccordo piatto)
  peak >= 1.25      -> granular_D(peak)       (= D, fascia alta)
  poi max(.., lock TP-aware 80/90%)           (invariato)
```
Valori: 0.5R→−0.25, 0.75R→BE, 1.0R→+0.25, 1.25R→+0.25 (riaggancia D), 1.5R→+0.5.
Confronto a tre: **D** (baseline), **minimal**, **gap 0.25** (riferimento
aggressivo).

## Pre-registrazione (predizioni, prima degli esiti)

- **Task 2**: quasi tutto il vantaggio di gap0.25 viene dalla fascia bassa; sui
  trend gap0.25 ha delta negativo; la minimal cattura la fascia bassa con **delta
  ≈ 0 sulla fascia alta** (non sacrifica i trend).
- **Task 3** (rumore-aware = fill SL sul lato peggiore della candela + spread):
  la minimal **perde poco**, gap0.25 perde molto; se la minimal mantiene il
  vantaggio su D rumore-aware è candidata forward.
- **Task 4**: la minimal batte D **sia con sia senza** #42/#49.

## Task 2 — Scomposizione del vantaggio per fascia (modalità base, in R)

| fascia (peak_R) | n | D | minimal | gap025 | Δmin vs D | Δgap vs D |
|-----------------|---|-----|---------|--------|-----------|-----------|
| sotto (<0.5) | 11 | −4.05 | −4.05 | −4.05 | +0.00 | +0.00 |
| bassa (0.5-1.0) | 6 | −2.50 | −1.00 | +2.29 | **+1.50** | **+4.79** |
| alta (≥1.0) | 10 | +10.28 | +8.51 | +6.54 | **−1.77** | **−3.74** |

Predizione **parzialmente confermata, parzialmente falsificata**:
- confermato che il vantaggio LORDO di gap0.25 è nella fascia bassa (+4.79), e
  che sui trend gap0.25 perde (−3.74);
- **falsificato** che la minimal non tocchi la fascia alta: la minimal perde
  **−1.77R sui trend**. Causa: il raccordo a 1.0-1.25R mette lo SL a +0.25 dove
  D lo tiene al BE, quindi i trend che passano per 1.0-1.25R e ritracciano un
  attimo vengono chiusi a +0.25 invece di essere lasciati correre al TP.

Netto minimal vs D = +1.50 (bassa) − 1.77 (alta) = **−0.27R**: la minimal è
**peggio di D** sul sample completo.

## Task 3 — Rumore-aware (il gate di questo giro)

Modello: all'hit dello SL, fill al lato peggiore della candela (gap-through,
conservativo) + costo spread (spread_r medio 0.0125R). Totali in R:

| strat | R base | R noise | perso | Δ vs D base | Δ vs D noise |
|-------|--------|---------|-------|-------------|--------------|
| D | 3.73 | 2.07 | −1.66 | +0.00 | +0.00 |
| minimal | 3.46 | 1.39 | −2.07 | **−0.27** | **−0.68** |
| gap025 | 4.78 | 2.46 | −2.32 | +1.05 | **+0.39** |

Predizione **falsificata**: la minimal **non** perde poco, perde 2.07R (più di D)
e col rumore scende a −0.68R sotto D. Il gap0.25 invece, pur perdendo il massimo
in assoluto (−2.32), **resta sopra D anche rumore-aware** (+0.39): il suo
vantaggio sopravvive al modello di rumore conservativo. Il caveat "gap stretto
fragile al rumore" del giro precedente si ridimensiona.

## Task 4 — make-or-break #42/#49 (senza i due Brent)

| strat | R base | R noise | Δ vs D base | Δ vs D noise |
|-------|--------|---------|-------------|--------------|
| D | 1.87 | 0.45 | +0.00 | +0.00 |
| minimal | 2.31 | 0.37 | +0.44 | **−0.08** |
| gap025 | 3.63 | 1.38 | +1.76 | **+0.93** |

La minimal batte D solo nello scenario "senza Brent, base" (+0.44); negli altri
tre è ≤ D, e **col rumore è ≤ D in entrambi gli scenari** (−0.68 completo, −0.08
senza Brent). Non supera il make-or-break in modo robusto. gap0.25 batte D in
tutti e quattro.

## Task 5 — Fascia bassa e pre-registrazione forward

### Baseline: trade fascia bassa (peak 0.5-1.0R, no full TP), n=6 (#43,44,54,57,62,63)

| strat | exit_R medio (base) | exit_R medio (noise) |
|-------|---------------------|----------------------|
| D | −0.417 | −0.534 |
| minimal | −0.167 | −0.353 |
| gap025 | +0.382 | +0.140 |

Il buco è **reale**: D porta a casa in media −0.42R su questi trade. La minimal lo
migliora (+0.25R base, +0.18R noise) ma li lascia comunque in perdita media
(−0.17R); solo gap0.25 li porta in positivo. Quindi la protezione anticipata
funziona sulla fascia bassa, ma la minimal ne cattura troppo poco e lo paga sui
trend.

### Pre-registrazione forward (struttura, da attivare solo su una variante promossa)

NB: dato il verdetto negativo sulla minimal, questa forward **non si attiva su di
essa**; resta come template per una variante futura pre-registrata che non tocchi
la fascia 1.0-1.25R.

- **(a) Metrica**: exit_R medio dei trade forward che toccano [0.5,1.0)R senza
  full TP, sotto la regola deployata, confrontato col controfattuale D
  (ricostruibile dall'`intra_trade_extreme` nativo).
- **(b) Predizione/soglia**: la regola candidata deve portare l'exit_R medio
  fascia-bassa da −0.42R (baseline D storico) ad **almeno −0.10R** (miglioramento
  ≥ +0.3R) **senza** ridurre l'expectancy totale, cioè exit_R medio dei trade con
  peak ≥ 1.25R **non** inferiore a D (controllo anti-taglio-trend).
- **(c) Sample minimo**: ≥ 10 trade forward in fascia bassa; sotto questa soglia
  il risultato resta "preliminare".
- **(d) Conferma**: exit_R fascia-bassa ≥ −0.10 **e** expectancy totale ≥ D su
  ≥10 trade. **Smentita**: nessun miglioramento fascia-bassa, oppure expectancy
  totale < D (trend tagliati), oppure i falsi stop-out erodono il guadagno.

## Verdetto gate: la minimal fix merita un deploy gated? NO

La minimal fix come specificata è **peggio di D** sul sample completo sia base
(−0.27R) sia rumore-aware (−0.68R), e supera il make-or-break solo in uno
scenario su quattro (senza Brent, base). Causa diagnosticata: il raccordo a
1.0-1.25R la rende più stretta di D all'inizio della fascia alta e **taglia i
trend nascenti** più di quanto la protezione della fascia bassa renda. **Niente
deploy.**

Due acquisizioni costruttive (non rivendicate come nuovi positivi, da
pre-registrare se si prosegue):
1. Il buco fascia bassa è reale e tappabile (D −0.42R → migliorabile), ma serve
   una variante che protegga 0.5-1.0R **senza** alzare l'aggancio nella fascia
   1.0-1.25R (cioè BE a 1.0 come D, non +0.25). Questa è una spec diversa, da
   pre-registrare a parte: non la chiamo "la vera minimal" per non fare HARKing.
2. gap0.25 resta la cosa migliore anche rumore-aware (+0.39R completo, +0.93R
   senza Brent): il suo vantaggio non è un artefatto della microstruttura
   idealizzata, almeno sotto questo modello di rumore. Resta però regime-
   dipendente (sacrifica i trend) come visto in `sprint4-trailing-calibration`.

## Limiti

Sample 27 trade (6 in fascia bassa). Il modello rumore-aware (fill al lato
peggiore della candela) è pessimistico sullo stop fisso iniziale dei rapid loss,
ma penalizza tutte le strategie allo stesso modo lì, quindi il **delta** fra
strategie resta informativo (il livello assoluto del noise no). Granularità 5m.
Il sample 38-64 è esaurito: la prova definitiva è forward, da qui la
pre-registrazione del Task 5.
