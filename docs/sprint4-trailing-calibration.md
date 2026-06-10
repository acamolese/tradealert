# Sprint 4 — Calibrazione trailing: quanto stretto seguire i winner

Data: 2026-06-10. Simulazione offline con gate pre-registrato, sola lettura
(candele Capital + DB anon). Nessuna modifica al sistema, monitor invariato.
Script: `jobs/trailing_calibration.py`.

**Sintesi in una riga**: per la prima volta in questa serie il verdetto NON è
negativo. Un trailing più **stretto** (gap 0.25R) batte l'opzione D attuale in
modo robusto al test #42/#49 (anzi vince di più senza i due Brent), perché D non
protegge la fascia 0.5-1.0R dove molti trade toccano profitto e poi ritracciano.
MA la predizione "ottimo intermedio" era sbagliata, il gap stretto è il più
esposto all'over-optimism da simulazione, e l'ottimo dipende dall'asset/regime.
Verdetto: **sì provvisorio**, da validare forward, non da deployare a scatola
chiusa.

## Obiettivo e metodo

Trovare la distanza di trailing che massimizza il profitto sui trade chiusi,
fra "seguire stretto" (blocca presto, rischia di tagliare le code dei trend) e
"seguire largo" (lascia respirare, restituisce sui pull-back).

Motore (pre-registrato): per ogni trade chiuso ricostruisco la traiettoria dalle
candele 5m (fallback 15m) Capital sulla vita reale `opened->closed`. Simulo
l'uscita come **primo tra** SL-trailato colpito, TP colpito, close ultima
candela. Le chiusure MANUALI reali sono ignorate (si misura il trailing in
autonomia), quindi i P&L simulati non replicano i reali. Short chiude all'ask,
long al bid; se una candela colpisce sia SL sia TP si assume SL (conservativo).
L'offset di ogni candela usa il picco delle candele PRECEDENTI (niente
look-ahead intra-candela).

## Task 1 — Copertura

Trade chiusi 38-64: **27. Simulati: 27. Scartati: 0.** Candele 5m disponibili per
tutti, inclusi i Sprint 2 di fine maggio (#42 del 22/05, #49 del 28/05). Nota:
`intra_trade_extreme` nativo esiste solo da #59, ma la ricostruzione da candele
copre l'intero range ed è più fine (5m vs snapshot 30min), quindi è la base usata.

## Pre-registrazione (scritta PRIMA degli esiti)

- **Baseline**: opzione D replicata dal codice (`_trailing_offset_r`):
  granularità 0.25R da 0.5R (offset −0.5 a 0.5R, BE a 1.0R, +0.25 a 1.25R, ...)
  + lock TP-aware (≥80% del cammino → 0.45·rr; ≥90% → 0.65·rr). Calcolando gli
  offset, **D è di fatto un trailing a gap ~1.0R più il lock vicino al TP**.
- **4 varianti**, griglia trail-gap fissa (dichiarata, non ottimizzata): quando
  il picco tocca 0.5R, `offset_R = max(−1.0, peak_R − G)`, senza lock TP-aware,
  per G ∈ {0.25, 0.50, 0.75, 1.0}R.
- **Predizione**: ottimo **intermedio** (0.5-0.75R); 0.25 taglia le code, 1.0
  restituisce troppo; curva concava.
- **Promozione (make-or-break)**: una variante è promossa solo se batte D **sia
  sul sample completo sia senza #42/#49**.

## Esiti

### Totali in R (e in €)

| strategia | R tot completo | € tot | R tot senza #42/#49 | € tot |
|-----------|----------------|-------|---------------------|-------|
| D (attuale) | 3.73 | +5.06 | 1.87 | **−3.63** |
| **gap 0.25** | **4.78 (+1.05)** | +13.46 | **3.63 (+1.76)** | +7.98 |
| gap 0.50 | 3.50 (−0.23) | +4.69 | 2.37 (+0.50) | −0.69 |
| gap 0.75 | 3.86 (+0.13) | +5.58 | 2.91 (+1.04) | +1.12 |
| gap 1.0 | 2.52 (−1.21) | +2.11 | 0.34 (−1.53) | −8.48 |

### Lettura

- **La predizione è falsificata**: l'ottimo non è intermedio, è il gap più
  **stretto** (0.25R). La curva non è concava: gap 1.0 (≈ D senza lock) è il
  peggiore, lo stretto il migliore.
- **gap 0.25 supera il make-or-break**: batte D sul completo (+1.05R) e **di più
  senza i Brent** (+1.76R). #42/#49 sono trend che corrono dritti (#42 peak
  2.93R, MAE −0.03), dove lo stretto chiude presto (0.78R) e il largo prende
  tutto (2.0R): i due Brent **penalizzano** la variante vincente, non la
  gonfiano. È l'opposto dell'artefatto visto in score/volatilità/chasing/regime.
- gap 0.75 batte D in entrambi gli scenari (più debole). gap 0.50 solo senza i
  Brent. gap 1.0 perde sempre.

### Perché lo stretto vince: il buco 0.5-1.0R di D

D, a 0.5R di profitto, mette l'offset a −0.5R: lo SL è ancora a **mezza perdita**.
Non protegge nulla finché il profitto non supera ~1.0R. Sul sample ci sono molti
trade che toccano 0.5-0.9R e poi ritracciano: D li lascia tornare a −0.5R, il
gap 0.25 blocca a `peak−0.25`. Esempi (exit_R, D vs gap0.25):

| trade | peak_R | D | gap 0.25 |
|-------|--------|---|----------|
| #43 Gold long | 0.52 | −0.50 | +0.27 |
| #44 Brent long | 0.68 | −0.50 | +0.42 |
| #57 Gold long | 0.89 | −0.25 | +0.63 |
| #62 Brent short | 0.52 | −0.50 | +0.27 |
| #63 Nasdaq short | 0.61 | −0.50 | +0.35 |

È esattamente il "buco fascia bassa" già segnalato in
`docs/sprint2-mfe-analysis.md`. Specularmente, lo stretto sacrifica i trend
forti (#42 2.0→0.78, #52 2.0→0.40, #58 2.08→0.78): su un sample choppy i
"quasi-winner che ritracciano" sono più numerosi dei trend, quindi lo stretto
vince. È un trade-off di **regime**, non un free lunch.

## Task 5 — Dipendenza dall'asset/volatilità

R totale per asset e strategia (best in grassetto concettuale):

| asset | n | D | gap0.25 | gap0.5 | gap0.75 | gap1.0 | best |
|-------|---|-----|---------|--------|---------|--------|------|
| Brent | 11 | 0.15 | **1.52** | 0.62 | −0.56 | −0.33 | gap0.25 |
| Gold | 6 | −1.64 | **−0.03** | −0.58 | −0.63 | −1.38 | gap0.25 |
| Nasdaq | 8 | **3.37** | 3.05 | 1.61 | 3.20 | 2.38 | D |
| Bitcoin | 1 | **2.00** | 0.40 | 2.0 | 2.0 | 2.0 | D/largo |
| US500 | 1 | −0.15 | −0.15 | — | — | — | pari (rapid loss) |

I dati **suggeriscono** che la distanza ottimale dipende dall'asset: Gold e
Brent (volatili, choppy nel periodo) preferiscono lo stretto; Nasdaq e Bitcoin
(trend più puliti) preferiscono D/largo. n per asset è piccolo (Bitcoin e US500
= 1): è un indizio, non lo ottimizzo.

## Caveat che rendono il "sì" provvisorio

1. **Over-optimism da simulazione, massimo proprio sul gap stretto.** La sim usa
   candele 5m e assume esecuzione dell'SL al prezzo, senza rumore tick, spread
   variabile o slippage. Un trailing a 0.25R nella realtà verrebbe stoppato dal
   rumore molto più spesso, con uscite premature che la sim non vede. È la
   variante più fragile rispetto alla microstruttura reale. Il vantaggio va
   confermato con una sim tick-level o, meglio, forward.
2. **Predizione sbagliata = comprensione incompleta.** Aspettarsi l'ottimo
   intermedio e trovare lo stretto segnala che il meccanismo dominante (il buco
   0.5-1.0R di D) non era previsto: motivo di cautela in più.
3. **Regime-dipendenza.** gap0.25 ottimizza per mercato choppy (questo periodo);
   in fasi di trend forte (più #42/#49) lascerebbe molto sul tavolo. Non è
   "stretto meglio in assoluto", è "stretto meglio in choppy".
4. Sample 27 trade, ~10 rapid loss che non differenziano fra strategie, 4 trend,
   ~9 quasi-winner che ritracciano: il risultato dipende dalla prevalenza di
   questi ultimi, specifica del periodo whipsaw maggio-giugno.

## Verdetto gate

**Esiste una calibrazione che batte D in modo robusto al test #42/#49? SÌ
(provvisorio).** gap 0.25R batte D sul completo (+1.05R, +8.4€) e senza i due
Brent (+1.76R, +11.6€), superando il make-or-break; gap 0.75R lo segue. È il
**primo risultato non-negativo** dopo score, volatilità, chasing e regime, ed è
coerente: quelle erano leve di **selezione entry** (morte su #42/#49), questa è
una leva di **gestione**, e supera proprio quel test.

Ma NON è azionabile a scatola chiusa: la predizione era sbagliata, il gap stretto
è il più esposto all'over-optimism della sim, e l'ottimo dipende da asset/regime.
**Prossimo passo proposto** (non deploy): (a) sim tick-level/spread-aware per
stimare quanto del vantaggio sopravvive al rumore reale, e/o (b) validazione
forward del lock anticipato nella fascia 0.5-1.0R (la modifica minima e meno
aggressiva del gap pieno: anticipare il primo lock sopra il BE, come già
ipotizzato in `sprint2-mfe-analysis`), pre-registrata. Non toccare le soglie ora.

## Limiti

Sample 27 trade. Chiusure manuali reali ignorate (sim autonoma): i P&L simulati
non sono i reali. Granularità 5m: micro-struttura intra-5min non vista. Capital
candele storiche soggette a disponibilità. Esiti per asset con n piccolo.
