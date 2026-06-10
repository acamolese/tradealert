# Sprint 4 Pilastro 1 — Regime filter: ricerca offline con gate pre-registrato

Data: 2026-06-10. Ricerca offline, sola lettura del DB (anon key), nessuna
modifica al sistema, monitor invariato. Gate pre-registrato PRIMA di calcolare
gli esiti. Sample: trade 38-65 (27 chiusi + 1 aperto escluso dal P&L).

**Sintesi in una riga**: il regime di mercato all'entry **è** correlato col P&L,
ma (a) nel verso **opposto** all'ipotesi (i giorni direzionali sono andati
peggio, non meglio), (b) la separazione è un **artefatto di due soli trade**
(#42 e #49, i Brent short già noti), e (c) un filtro di regime taglierebbe **2
dei 4 TP pieni**. Gate **NEGATIVO**, come per volatilità e chasing.

## Ipotesi sotto test

Il sistema sarebbe un momentum-follower che perde strutturalmente in mercato
choppy (settimana 8-10/06: long l'8, short il 9-10, perdite in entrambe le
direzioni = whipsaw). Un filtro di regime a livello mercato/asset (NON
trade-level: quelle piste sono chiuse) che identifica i giorni "non-trending" e
li evita potrebbe cambiare segno all'expectancy.

## Pre-registrazione (scritta PRIMA di calcolare il P&L per regime)

### Metriche (3), da `features_at_decision`, livello asset/giornata all'entry

- **M1 — Efficienza direzionale** (PRINCIPALE, pre-scelta come decisiva):
  `|daily_pct_change| / daily_range_pct`. Quota del range giornaliero che si
  traduce in spostamento netto. Alto = giornata direzionale (trending); basso =
  molto range, poco netto (choppy). È la definizione canonica di choppiness.
- **M2 — Coerenza del trend** (supporto): `trend_slope_pct ×
  trend_slope_short_pct`. Positivo = i due orizzonti 4H (20 e 8 candele)
  concordano di segno (trending); negativo = discordano (choppy).
- **M3 — Forza del trend** (supporto): `|trend_slope_pct|`. Pendenza media
  assoluta; alto = trend forte, basso = piatto.

### Soglie a priori (non ottimizzate sugli esiti)

Terzili della metrica sul sample 38-65: choppy = sotto il 33° percentile,
trending = sopra il 67°, neutro in mezzo. Fissate sulla distribuzione della
metrica, non sul P&L.

### Predizione (cosa mi aspettavo)

Sulla metrica principale M1: trade aperti in giorni choppy → P&L cumulato
**negativo**; in giorni trending → **positivo**. I 4 TP pieni (#42, 49, 52, 58)
prevalentemente in trending; i loss della serie whipsaw in choppy.

### Regola anti multiple-testing e criterio gate (dichiarati prima)

Metrica decisiva = **solo M1**. M2/M3 triangolano, non salvano il gate.
Gate **POSITIVO solo se tutte**:
1. M1: P&L medio choppy < 0 **e** trending > 0 (separazione di **segno**);
2. la separazione di segno regge **anche sul solo Sprint 2 (38-58)**;
3. **al massimo 1** dei 4 TP pieni cade in choppy (il filtro non taglia i TP).
Se M1 non separa nel verso predetto, o se ≥2 TP pieni sono in choppy → NEGATIVO,
a prescindere da M2/M3. Niente "provo tutto finché una separa".

### Collegamento da verificare

Il R:R compresso (audit R:R: in regime volatile/evento scende verso il floor
1.2) correla coi giorni choppy? Se sì, il R:R all'entry sarebbe un proxy di
regime a costo zero.

## Esiti (calcolati dopo la pre-registrazione)

Terzili: M1 [0.459, 0.733], M2 [0.001, 0.019], M3 [0.089, 0.188].

### M1 — Efficienza direzionale (la decisiva)

| regime | n | P&L cum | P&L medio | win | TP pieni |
|--------|---|---------|-----------|-----|----------|
| choppy | 9 | **+14.52** | **+1.61** | 3 | **2** |
| neutro | 9 | +0.24 | +0.03 | 2 | 1 |
| trending | 9 | **−5.83** | **−0.65** | 2 | 1 |

corr M1 vs pnl = **−0.31** (n=27). Solo Sprint 2: choppy +1.87/trade, trending
−1.00/trade. **La separazione esiste ma con segno INVERTITO** rispetto alla
predizione: i giorni direzionali sono andati peggio, i choppy meglio.

### M2, M3 (supporto)

| | choppy | neutro | trending | corr vs pnl |
|--|--------|--------|----------|-------------|
| M2 (coerenza trend) | +0.21 | +0.93 | −0.06 | −0.08 |
| M3 (forza trend) | −0.06 | +1.58 | −0.79 | −0.13 |

Nessuna separazione monotòna utile: in M2 e M3 il terzile migliore è il
**neutro**, non un estremo. Niente struttura sfruttabile.

## Test di impatto sui TP pieni (il make-or-break)

| TP pieno | P&L | M1 | regime M1 |
|----------|-----|----|-----------|
| #42 Brent short | +10.85 | 0.352 | **choppy** |
| #49 Brent short | +9.46 | 0.004 | **choppy** |
| #52 Bitcoin short | +2.01 | 0.753 | trending |
| #58 Nasdaq short | +6.77 | 0.733 | neutro |

**2 dei 4 TP pieni cadono in choppy, e sono i due maggiori** (+20.31 insieme). La
condizione 3 del gate (≤1 TP pieno in choppy) è violata. Un filtro che evitasse
i giorni choppy taglierebbe #42 e #49: stessa identica dinamica del filtro
volatilità (troncone 2) e dello stesso paio di trade.

## Il driver: la separazione è un artefatto di due trade

I 9 trade "choppy" sommano +14.52, ma #42 (+10.85) e #49 (+9.46) da soli fanno
+20.31. **I restanti 7 trade choppy sommano −5.79**: senza i due Brent short, il
regime choppy è in perdita. Quindi "choppy profittevole" non è un segnale di
regime, è di nuovo #42 e #49 che dominano ogni taglio di questo sample (come per
score, volatilità, chasing). Non c'è un effetto-regime robusto né nel verso
ipotizzato né in quello inverso.

## Collegamento R:R

corr R:R vs M1 = **−0.27**: il R:R si comprime nelle giornate **più
direzionali** (M1 alto), non in quelle choppy. Coerente con l'audit R:R (le
giornate a forte spostamento netto sono volatili → SL largo → R:R basso). Quindi
il R:R compresso è proxy di "giornata direzionale/volatile", **non** di choppy:
il collegamento ipotizzato (R:R basso = choppy) è anch'esso smentito.

## Verdetto gate: NEGATIVO

Tutte e tre le condizioni pre-registrate falliscono:
1. M1 separa, ma nel **verso opposto** (choppy +1.61, trending −0.65): l'ipotesi
   "il sistema perde in choppy" è **falsificata**.
2. Su Sprint 2 da solo la separazione è invertita allo stesso modo.
3. **2 dei 4 TP pieni** (i due maggiori) sono in choppy: il filtro li taglierebbe.

Inoltre la separazione di M1 è dominata da 2 trade (#42, #49); rimossi, sparisce.
M2 e M3 non separano. Il regime di mercato all'entry **non è un filtro
sfruttabile** per questa expectancy.

### Sull'ipotesi invertita (NON la rivendico)

Il fatto che M1 separi al contrario (entrare in giornate già direzionali è andato
peggio) è coerente col filone precedente (entrare quando il movimento è già
avvenuto = entry maturo = peggio), ma **non lo dichiaro come risultato
positivo**: sarebbe esattamente l'HARKing / multiple-testing che il gate doveva
prevenire, ed è comunque un artefatto di 2 trade. Resta al più un'ipotesi
generativa da pre-registrare e validare **forward** separatamente, non una
conferma del Pilastro 1, che è chiuso negativo.

## Limiti

Sample 28 trade (27 con P&L), Brent 11, fortemente influenzato da #42/#49.
Metriche da `features_at_decision` (regime dell'asset al momento dell'entry, non
un indice di regime di mercato indipendente). `peak_R`/esiti influenzati anche
da trailing e chiusure manuali. Le correlazioni su ~27 punti hanno intervalli
ampi. Come per chasing, il sample è esaurito come fonte di prova: un eventuale
seguito richiede materiale forward.
