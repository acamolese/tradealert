# Sprint 2 — Analisi "profit non protetto"

Data: 2026-06-04. Perimetro: id 38-55, **18 trade chiusi** (Sprint 2
completo, fix bidirezionale in produzione dal 2026-05-20, primo trade
id 38 / signal 85). Tutti chiusi alla data dell'analisi.

Obiettivo: dimensionare in modo netto il problema identificato il 3/06
(trailing con SL ancorato a breakeven nella fascia 1.0-1.5R, primo lock
sopra il BE solo a +1.5R). Quante volte siamo arrivati vicini al TP o a
un guadagno ragionevole e poi siamo usciti a profit ridotto, in perdita o
a breakeven. Non è una proposta di fix, solo la quantificazione.

## Metodo

Job dedicato `jobs/peak_analysis.py`. Per ogni trade estrae le candele
storiche fini da Capital (MINUTE_5, fallback MINUTE_15 quando la finestra
è troppo ampia per la granularità a 5m) sull'intera vita del trade, da
`opened_at` a `closed_at`, e ricostruisce il **peak profit flottante
realmente incassabile**:

- LONG: peak su `max(highPrice.bid)` (un long si chiude al bid)
- SHORT: peak su `min(lowPrice.ask)` (uno short si chiude all'ask)

Rischio (1R) = `|entry - SL_originale| * size`, con SL originale derivato
dalla percentuale `stop_loss` del signal (il `current_sl` del trade è già
trailato e non rappresenta il rischio all'ingresso). Reward e "% verso
TP" usano il `current_tp` del trade (il TP non viene mai spostato, quindi
è stabile e autorevole).

### Caveat metodologici (da tenere presenti leggendo i numeri)

1. **High/low di candele 5m/15m è una proxy del peak vero.** Tra un tick
   e l'altro il prezzo può aver toccato un estremo non catturato dalla
   candela: la proxy può sottostimare (o, raramente, sovrastimare di
   poco) il picco reale. Il trade 48 usa MINUTE_15 (finestra di 3,4
   giorni), peak leggermente più grezzo degli altri.
2. **"Incassare al peak" è ipotetico.** Nessun sistema reale incassa al
   massimo: richiederebbe foresight. I numeri di "profit lasciato sul
   tavolo" e il delta finale dell'Output #3 sono un **upper bound** del
   recupero teoricamente possibile, non un obiettivo raggiungibile.

## Output #1 — Tabella per trade

| id | asset | dir | R:R | Peak USD | Peak R | Peak % vs TP | Final USD | Give-back USD | Give-back % | Exit |
|----|-------|-----|-----|----------|--------|--------------|-----------|---------------|-------------|------|
| 38 | Brent Oil | short | 1.32 | 1.42 | 0.28 | 21.6% | -0.88 | 2.30 | 162% | manual |
| 39 | Nasdaq 100 | long | 1.49 | 1.42 | 0.38 | 25.5% | -0.79 | 2.21 | 156% | manual |
| 40 | Nasdaq 100 | long | 1.41 | 4.47 | 1.12 | 79.9% | +4.16 | 0.31 | 6.9% | manual |
| 41 | Gold | long | 2.19 | 0.35 | 0.13 | 5.8% | -0.19 | 0.54 | 155% | manual |
| 42 | Brent Oil | short | 1.99 | 14.36 | 2.93 | 147% | +10.85 | 3.52 | 24.5% | **tp_hit** |
| 43 | Gold | long | 2.28 | 1.00 | 0.52 | 22.8% | -1.93 | 2.93 | 294% | stop_hit |
| 44 | Brent Oil | long | 2.07 | 3.28 | 0.68 | 32.6% | -2.44 | 5.72 | 174% | stop_hit |
| 45 | Nasdaq 100 | long | 2.21 | 3.44 | **1.28** | 57.6% | **0.00** | 3.44 | 100% | stop_hit |
| 46 | Brent Oil | short | 1.35 | 2.24 | 0.45 | 33.6% | -2.52 | 4.76 | 213% | manual |
| 47 | Gold | short | 2.13 | -0.35 | -0.09 | -4.1% | -4.00 | 3.65 | n/d | stop_hit |
| 48 | Nasdaq 100 | long | 2.25 | 3.06 | **1.41** | 62.7% | +2.82 | 0.24 | 8.0% | manual |
| 49 | Brent Oil | short | 2.12 | 9.70 | 2.17 | 103% | +9.46 | 0.24 | 2.5% | **tp_hit** |
| 50 | Brent Oil | short | 1.99 | 0.53 | 0.11 | 5.4% | -2.98 | 3.52 | 658% | manual |
| 51 | Brent Oil | long | 1.42 | 5.48 | **1.16** | 81.4% | -0.11 | 5.58 | 102% | stop_hit |
| 52 | Bitcoin | short | 1.99 | 2.10 | 2.08 | 104% | +2.01 | 0.09 | 4.2% | **tp_hit** |
| 53 | Gold | long | 1.51 | 0.37 | 0.08 | 5.4% | -1.59 | 1.96 | 532% | manual |
| 54 | Nasdaq 100 | long | 2.25 | 1.70 | 0.77 | 34.4% | -1.10 | 2.80 | 165% | stop_hit |
| 55 | Brent Oil | long | 1.32 | 5.33 | **1.09** | 82.1% | -0.06 | 5.39 | 101% | stop_hit |

Nota: give-back % > 100% quando il finale è negativo (si è restituito
tutto il peak e oltre). Trade 47: il peak è negativo (Gold short mai
andato a favore neppure sui low 5m), give-back % non definito.

## Output #2 — Sintesi su 3 soglie

### Soglia bassa: peak ≥ +0.5R

- Trade che superano la soglia in peak: **11** (40, 42, 43, 44, 45, 48, 49, 51, 52, 54, 55)
- Di questi, usciti sotto il peak (give-back > 0): **11 / 11**
- Split: **5** usciti a profit ridotto (40, 42, 48, 49, 52) — **6** usciti in perdita o BE (43, 44, 45, 51, 54, 55)
- Profit lasciato sul tavolo (somma peak − final): **30.3 USD**

### Soglia media: peak ≥ +1.0R

- Trade che superano la soglia in peak: **8** (40, 42, 45, 48, 49, 51, 52, 55)
- Di questi, usciti sotto il peak: **8 / 8**
- Split: **5** a profit ridotto (40, 42, 48, 49, 52) — **3** in perdita o BE (45, 51, 55)
- Profit lasciato sul tavolo: **18.8 USD**
- I 3 casi in perdita/BE sono la prova più pulita del bug: hanno toccato
  ≥ +1R di profit (45 +1.28R, 51 +1.16R, 55 +1.09R) e sono usciti a zero.
  Give-back scommesso solo da questi tre: **14.4 USD**.

### Soglia alta: peak ≥ 70% del cammino verso il TP

- Trade che superano la soglia in peak: **6** (40, 42, 49, 51, 52, 55)
- Di questi, usciti sotto il peak: **6 / 6**
- Split: **4** a profit ridotto (40, 42, 49, 52) — **2** in perdita o BE (51, 55)
- Profit lasciato sul tavolo: **15.1 USD**
- I 2 casi in perdita/BE (51 e 55, entrambi Brent long) sono i più
  vistosi: arrivati all'**81-82% del cammino verso il TP**, oltre +5 USD
  di profit flottante, e usciti a breakeven. Give-back su questi due:
  **11.0 USD**.

Lettura: nelle tre soglie il give-back è universale (chi supera la soglia
esce sempre sotto il peak, com'è atteso). La parte che pesa davvero è la
quota uscita in **perdita o BE** dopo essere stata in chiaro profitto: 6
trade alla soglia bassa, 3 alla media, 2 all'alta. Sono i trade che il
trailing non ha protetto perché lo SL era inchiodato a breakeven mentre
il prezzo viaggiava nella fascia 1.0-1.5R / 57-82% del TP.

Avvertenza sui profit ridotti: i trade usciti comunque in utile che
gonfiano il "lasciato sul tavolo" sono in larga parte tp_hit (42, 49, 52)
dove il peak supera il TP solo per overshoot di pochi tick oltre il
target. Lì il give-back è fisiologico (il sistema ha incassato al target
previsto), non una lacuna.

## Output #3 — Il numero per Andrea

Sommando sui 18 trade Sprint 2:

- P&L realmente portato a casa dal sistema: **+10.71 USD**
- P&L se ogni trade fosse stato incassato al peak osservato: **+59.89 USD**
- **Differenza (profit lasciato sul tavolo, upper bound): ≈ +49.2 USD**

Il sistema ha incassato circa un sesto di quanto i picchi avrebbero
permesso nel migliore dei mondi. Da leggere come **upper bound teorico**
(richiede foresight perfetto, vedi caveat 2), non come recupero
ottenibile. Una stima più realistica del recuperabile è quella
dell'Output #2: i trade andati ≥ +1R e usciti a perdita/BE hanno
restituito da soli **14.4 USD**, ed è la fascia dove un lock di profit
anche modesto avrebbe agito.

## Pattern emersi

- I give-back peggiori in valore assoluto (44, 51, 55, 46) sono tutti su
  **Brent** con size grande (1.6-2.1): lì ogni decimo di R vale di più in
  USD, quindi il buco di protezione costa di più.
- I 4 trade usciti `stop_hit` dopo aver superato +1R o il 57% del TP (45,
  51, 55, più 54 vicino) sono il cuore del problema: SL trailato a
  breakeven, prezzo che rientra, uscita a zero.
- I trade con peak basso (< 0.5R: 38, 39, 41, 46, 47, 50, 53) non sono
  give-back di protezione ma entry deboli, andati poco o subito contro.
  Restano territorio dell'analisi reattività, non del salva-profit.
