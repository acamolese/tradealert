# Sprint 4 — Trailing V1 "fascia bassa pulita": la spec corretta

Data: 2026-06-10. Analisi offline + sim rumore-aware + make-or-break +
attivazione pre-registrazione forward. Sola lettura (candele Capital + DB anon),
monitor invariato, **nessun deploy in questo giro**. Gate pre-registrato prima
degli esiti. Script: `jobs/trailing_v1_clean.py`. Sample: 27 trade chiusi 38-64.

**Sintesi in una riga**: V1 **passa**. Protegge la fascia 0.5-1.0R e si riaggancia
a D al breakeven a 1.0R, quindi **non tocca un solo trend** (delta-trend = 0
esatto, il difetto che aveva bocciato la minimal). Batte D in tutte e quattro le
celle del make-or-break (+1.46/+1.61/+1.46/+1.60 R), in modo stabile con e senza
i Brent e robusto al rumore. È la prima variante che merita un **deploy gated**
con validazione forward.

## Task 1 — Spec V1 (pre-registrata)

```
offset_V1(peak):
  peak < 0.5         -> -1.0                  (no trailing, come D)
  0.5 <= peak < 0.75 -> rampa -0.25 -> -0.10
  0.75 <= peak < 1.0 -> rampa -0.10 -> 0.0 (BE)
  peak >= 1.0        -> offset_D(peak)        (D PURO: granular + lock TP-aware)
```
Punti: 0.5R→−0.25, 0.75R→−0.10, 1.0R→BE (= D). Unica differenza con la minimal
bocciata: a 1.0R riaggancia D al **breakeven**, niente raccordo a +0.25 in
1.0-1.25R. Fascia ≥1.0R identica a D per costruzione. Confronto: **D** (baseline),
**V1**, **V2 = gap 0.25 puro** (riferimento aggressivo).

## Pre-registrazione (predizioni)

- V1 fascia alta (peak ≥ 1.25R): **delta vs D ≈ 0** (controllo che la minimal ha
  fallito).
- V1 fascia bassa: delta positivo, ma minore di gap0.25 (aggancio più largo).
- V1 netto: ≥ D, **mai sotto D** nelle 4 celle (criterio di promozione).
- V2 (gap0.25): vantaggio fascia bassa, sacrificio trend, netto regime-dipendente.

## Task 3 — Esiti

### Delta vs D per fascia (modalità base, R)

| fascia (peak_R) | n | D | V1 | gap025 | ΔV1 | Δgap |
|-----------------|---|-----|-----|--------|-----|------|
| sotto (<0.5) | 11 | −4.05 | −4.05 | −4.05 | +0.00 | +0.00 |
| bassa (0.5-1.0) | 6 | −2.50 | −1.04 | +2.29 | **+1.46** | +4.79 |
| alta (≥1.0) | 10 | +10.28 | +10.28 | +6.54 | **+0.00** | −3.74 |

Predizione **confermata**: V1 cattura +1.46R sulla fascia bassa e **+0.00R sulla
fascia alta** (delta-trend nullo). gap0.25 cattura di più in basso (+4.79) ma lo
paga in alto (−3.74).

### Controllo anti-taglio-trend (peak ≥ 1.25R, n=7: #42,45,48,49,52,58,59)

| strat | exit_R medio base | Δ vs D | noise |
|-------|-------------------|--------|-------|
| D | +1.143 | +0.000 | +1.070 |
| V1 | +1.143 | **+0.000** | +1.070 |
| gap025 | +0.666 | −0.477 | +0.633 |

**Trend con V1 ≠ D: NESSUNO.** V1 lascia i sette trend identici a D, per
costruzione e verificato empiricamente: nessun trend ritraccia abbastanza in
fascia bassa da far scattare lo SL V1 prima di ripartire. gap0.25 invece taglia i
trend del 42% (−0.477R medio).

### Fascia bassa (6 trade #43,44,54,57,62,63), exit_R medio

| strat | base | noise |
|-------|------|-------|
| D | −0.417 | −0.534 |
| V1 | **−0.174** | **−0.267** |
| gap025 | +0.382 | +0.140 |

V1 migliora la fascia bassa di +0.24R (base) e +0.27R (noise): la riporta vicino
al breakeven (la lascia leggermente negativa, non la trasforma in profitto come
gap0.25, ma senza il costo sui trend).

## Task 4 — Make-or-break: le quattro celle

| strat | base, con Brent | noise, con Brent | base, senza Brent | noise, senza Brent |
|-------|-----------------|------------------|-------------------|--------------------|
| D | 3.73 | 2.07 | 1.87 | 0.45 |
| **V1** | **5.19 (+1.46)** | **3.68 (+1.61)** | **3.33 (+1.46)** | **2.05 (+1.60)** |
| gap025 | 4.78 (+1.05) | 2.46 (+0.39) | 3.63 (+1.76) | 1.38 (+0.93) |

**V1 supera tutte e quattro le celle** (+1.46/+1.61/+1.46/+1.60), cosa che la
minimal aveva fatto in 1 su 4. Due proprietà chiave:
- **Stabile con/senza Brent** (+1.46 in entrambe le celle base): il vantaggio
  viene dalla fascia bassa, non dai trend, quindi non è regime-dipendente come
  gap0.25 (che oscilla +1.05/+1.76 base, +0.39/+0.93 noise).
- **Robusto al rumore**: il vantaggio cresce col rumore (+1.61 vs +1.46), perché
  V1 esce più in alto (BE/−0.10) e il fill al lato peggiore la erode meno di D.

Sul totale V1 è la migliore delle tre in tre celle su quattro (perde solo contro
gap0.25 in "base senza Brent", 3.33 vs 3.63, ma lì col rumore V1 torna avanti
2.05 vs 1.38).

## Task 5 — Verdetto e attivazione forward

### Verdetto: V1 merita un deploy gated con forward? SÌ

V1 soddisfa tutti i criteri pre-registrati: delta-trend = 0 esatto (risolve il
difetto della minimal), batte D in tutte e quattro le celle, è robusta al rumore
e non regime-dipendente. È un miglioramento **difensivo e "gratis"**: recupera
valore nella fascia 0.5-1.0R dove D lascia tornare lo SL a −0.5R, senza alcun
costo sui trend. Non è un game-changer dell'expectancy (in-sample +1.46R su 27
trade, concentrato in 6 trade fascia-bassa), ma è il primo intervento che cattura
valore senza effetti collaterali.

### Forward pre-registrata (attivata su V1)

Template Task 5 di `sprint4-trailing-minimal-fix.md`, calibrato sull'in-sample
di V1:
- **Metrica**: exit_R medio dei trade forward che toccano [0.5,1.0)R senza full
  TP, sotto V1 vs controfattuale D (ricostruibile da `intra_trade_extreme`).
- **Predizione**: V1 migliora l'exit_R medio fascia-bassa di **≥ +0.20R** rispetto
  al controfattuale D (in-sample fa +0.24/+0.27R), su **≥ 10 trade forward** in
  fascia bassa; sotto questa soglia resta "preliminare".
- **Controllo anti-taglio-trend forward**: exit_R medio dei trade con peak ≥1.25R
  sotto V1 **non inferiore** a D (atteso: identico, delta ≥ −0.05R).
- **Conferma**: miglioramento fascia-bassa ≥ +0.20R **e** delta-trend ≥ −0.05R su
  ≥10 trade. **Smentita**: nessun miglioramento fascia-bassa, oppure i trend
  vengono tagliati (delta-trend < −0.05R), oppure l'expectancy totale scende
  sotto D.

### Deploy gated proposto (da decidere insieme, non eseguito)

Modifica chirurgica a `_trail_offset_granular`/`_trailing_offset_r` in
`src/position_monitor.py`: nella sola fascia 0.5≤peak<1.0 sostituire l'offset
granulare con la rampa V1 (−0.25→BE), lasciando invariati il ramo ≥1.0R e il lock
TP-aware. Dietro flag (es. `TRAIL_V1_LOWBAND`), reversibile come gli altri
esperimenti, con il logging del controfattuale D per la metrica forward. **Nessun
cambio finché non leggiamo il doc insieme.**

## Confronto V1 vs gap0.25 (V2)

gap0.25 resta competitivo solo in mercato choppy puro (base, senza Brent: 3.63 vs
V1 3.33), ma: taglia i trend (−0.477R), è regime-dipendente, e col rumore cede a
V1 in ogni cella. V1 è la scelta robusta; gap0.25 sarebbe un azzardo direzionale
sul regime. Nessun HARKing: V1 è esattamente la spec pre-registrata, non una
terza variante emersa dagli esiti.

## Limiti

Sample 27 trade, il vantaggio di V1 viene da 6 trade fascia-bassa: robusto alle
celle ma su pochi dati. V1 riduce il danno della fascia bassa (−0.42→−0.17R) ma
non la porta in positivo. Modello di rumore = una sola assunzione (fill-worst-
side + spread), conservativa ma non esaustiva (no rumore tick puro). Il sample
38-64 è esaurito: la prova definitiva è la forward sopra.
