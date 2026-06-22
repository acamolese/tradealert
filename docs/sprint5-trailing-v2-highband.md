# Sprint 5 — Trailing V2 "fascia alta" (1.0-1.25R)

Data: 2026-06-22. Deploy gated, reversibile, analogo a V1. Tocca il trailing →
massima cura. Flag `TRAIL_V2_HIGHBAND` (default OFF = bit-identico).

**Sintesi**: D tiene lo SL a breakeven per tutta la fascia 1.0-1.25R e salta a
+0.25R solo a 1.25R, lasciando **restituire il picco** ai trade che culminano lì
(casi reali #76 picco 1.24R→BE→chiuso ~0, #79 picco 1.16R→BE→give-back in corso).
V2 colma il buco: rampa lo SL da BE a +0.25R attraverso 1.0-1.25R.

## La spec

```
offset_V2(profit_r) per 1.0 <= profit_r < 1.25:  0.0 + (profit_r-1.0)/0.25 × 0.25
```
1.0R→0.0 (=D), 1.10R→+0.10, 1.16R→+0.16, 1.24R→+0.24, 1.25R→+0.25 (=D). Riaggancia
D a entrambi gli estremi: nessun salto. Fuori da 1.0-1.25R resta D.

## V2 e V1 sono su fasce DISGIUNTE (non si toccano)

- V1 agisce 0.5-1.0R; V2 agisce 1.0-1.25R. Verificato: V1 a 1.16R == D, V2 a
  0.73R == D, V1+V2 a 0.73R == V1.
- **Il gate forward di V1** misura i trade che picchiano in 0.5-1.0R (exit_R vs
  controfattuale D). V2 tocca solo trade che superano 1.0R, che **non sono
  campioni del gate V1**. Quindi aggiungere V2 **non contamina** la validazione
  di V1: il vincolo "un esperimento alla volta" riguardava la misura di V1, e V2
  in una fascia disgiunta non la altera.
- Il controfattuale `offset_r_d` loggato resta **D puro** (V1 e V2 entrambi OFF),
  così sia il gate V1 sia quello V2 hanno la stessa baseline pulita.

## Prova bit-identica (OFF) e correzione (ON)

OFF (v1=v2=false) == D puro su tutta la griglia: **confermato**.
ON, fascia 1.0-1.25R:

| profit_r | D | V2 | delta |
|----------|-----|------|-------|
| 1.0 | 0.0 | 0.0 | = |
| 1.10 | 0.0 | +0.10 | +0.10 |
| 1.16 | 0.0 | +0.16 | +0.16 |
| 1.24 | 0.0 | +0.24 | +0.24 |
| 1.25 | +0.25 | +0.25 | = |

## Caso #79 (Brent short, live): cosa V2 avrebbe fatto, e l'onestà sul timing

#79 picco 1.16R → con V2 lo SL si sarebbe bloccato a **+0.16R** invece del
breakeven. MA: il trailing campiona il `profit_r` **istantaneo** ogni 5 min e lo
SL è solo-migliorativo. Quando #79 era nella fascia 1.0-1.25R V2 era OFF, quindi
non ha bloccato; ora #79 è ridisceso a ~0.96R (sotto 1.0R) e lo SL è già a
breakeven. **Accendere V2 adesso NON salva #79 retroattivamente**: il picco è
passato e a 0.96R V2 non agisce (fuori fascia). V2 protegge i **prossimi** trade
che entrano in 1.0-1.25R, non quello in corso. Onestà dovuta.

## Trade-off (come V1)

Bloccare a +0.16/+0.24R in fascia 1.0-1.25R protegge il give-back, ma se un trade
dippa fino al lock e poi sarebbe ripartito oltre 1.25R verso il TP, V2 lo taglia
prima. È la stessa scommessa runner-vs-giveback di V1, in una fascia più alta. Il
lock è modesto (≤0.25R) e la fascia è stretta. Da validare forward come V1.

## Gate forward V2 (pre-registrato)

Campioni = trade che picchiano in 1.0-1.25R **senza** raggiungere il TP. Metrica:
exit_R medio sotto V2 vs controfattuale D (loggato in `offset_r_d`). V2 "ha
ragione" se migliora l'exit_R medio di questi trade (protegge il give-back) senza
ridurre l'expectancy totale (controllo che non tagli i runner che poi vanno a TP:
exit_R medio dei trade peak≥1.25R non inferiore a D). Su ≥10 campioni fascia-alta.

## Guardrail

- Flag `TRAIL_V2_HIGHBAND` (default OFF). Rollback: `=false` nel `.env` VM.
- OFF = bit-identico a D (provato). Nessun trade aperto toccato in modo dannoso:
  il trailing è solo-migliorativo (lo SL può solo salire).
- `trail_variant` negli eventi distingue `v1_lowband` / `v2_highband` / `D` in
  base alla fascia al momento dell'evento, per l'analisi a posteriori.
- V1 e V2 indipendenti: ciascuno col proprio flag, validabili in parallelo.

## Limiti

Stesso campionamento a 5 min del trailing: il picco vero tra due tick non viene
catturato (V2 lavora sul profit_r istantaneo, non sul max). Lock modesto in una
fascia stretta → effetto per-trade piccolo (~0.1-0.25R); il valore è cumulativo
sui give-back ripetuti (Brent oversold). Validazione forward, non in-sample.
