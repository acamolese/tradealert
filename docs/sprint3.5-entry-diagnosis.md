# Sprint 3.5 — Diagnosi qualità entry (preliminare)

Data: 2026-06-06. Sola analisi, nessuna modifica al sistema. Sample: trade
chiusi Sprint 2 id 38-58 (**21 trade chiusi**; il sistema ne conta 20 come
Sprint 2, #58 è il checkpoint di chiusura, qui inclusi tutti i chiusi del
range). Dati: `signals.features_at_decision` (feature all'entry) + peak_R
ricostruito da candele storiche (`jobs/peak_analysis.py`).

Domanda centrale: perché ~8 entry su LOSS vanno contro nelle prime ore
(peak < +0.5R) mentre gli altri si sviluppano? Cosa li distingue?

## Definizione dei gruppi

- **RAPID_LOSS** (n=8): pnl < 0 e peak_R < 0.5 (mai realmente andati a
  favore). id: 38, 39, 41, 46, 47, 50, 53, 56.
- **DEVELOPED** (n=13): WIN, oppure LOSS/BE con peak_R ≥ 0.5 (almeno un
  peak). id: 40, 42, 43, 44, 45, 48, 49, 51, 52, 54, 55, 57, 58.

Coerenza col passato: i 7 rapid loss della profit-protection (38, 39, 41,
46, 47, 50, 53) ricadono tutti qui; si aggiunge solo #56 (Brent long,
nuovo nel range esteso). Il confine 0.5R è fuzzy: #43 (peak 0.52) e #46
(peak 0.45) sono praticamente a cavallo.

## ⚠️ Limiti statistici (leggere prima dei pattern)

- **Sample minuscolo**: 8 vs 13 su un solo sprint. Nessun test di
  significatività regge; qui si guardano direzioni e mediane, non p-value.
- **Medie inquinate da outlier**: es. #38 ha daily_pct_change −5.05. Uso
  le **mediane** come riferimento e segnalo gli outlier.
- **Feature set evoluto a metà Sprint 2**: `trend_slope_short_pct` manca
  sui primi signal (aggiunta in Fase 2a-bis), quindi ha n ridotto.
- **peak_R è proxy** (candele 5m/15m). I trade Sprint 3 avranno
  `intra_trade_extreme` nativo, più pulito: questa diagnosi va rifatta lì.
- **Macro non analizzata**: non ho un join affidabile col calendario
  storico (NFP/Ifo). Dimensione lasciata aperta (vedi sotto).

## Pattern trovati (ordinati per robustezza)

### 1. Volatilità all'entry più alta nei rapid loss (segnale più solido)

| feature (mediana) | RAPID_LOSS | DEVELOPED |
|-------------------|-----------|-----------|
| atr_pct_of_price | **1.32** | 0.70 |
| bb_width_pct | **5.60** | 2.89 |

I rapid loss entrano in regimi quasi 2× più volatili (ATR e ampiezza
Bollinger). È il contrasto più consistente sulle mediane. Intuizione: in
alta volatilità il rumore intraday colpisce lo stop prima che la tesi si
sviluppi. Non universale però: #41 e #53 (Gold) sono rapid loss a bassa
volatilità.

### 2. Posizione nella giornata: rapid loss più "in basso" / su down-day

| feature (mediana) | RAPID_LOSS | DEVELOPED |
|-------------------|-----------|-----------|
| pct_from_daily_high | **−0.91** | −0.33 |
| daily_pct_change | **−0.53** | +0.41 |

I rapid loss entrano più lontano dal massimo di giornata e più spesso in
una giornata già negativa per l'asset: **5/8 rapid loss su down-day (62%)
contro 3/13 developed (23%)**. Attenzione: non è "controtrend" (il check
controtrend intraday è 1/8 vs 1/13). È piuttosto **entrare su asset deboli
nella giornata**, spesso short che inseguono un ribasso già maturo (#38,
#46, #50 Brent short su giornate −5/−3%) e poi rimbalzano contro.

### 3. Lo score LLM NON discrimina (risultato negativo importante)

| feature (mediana) | RAPID_LOSS | DEVELOPED |
|-------------------|-----------|-----------|
| score | **7.25** | 7.00 |

I rapid loss avevano score uguale o leggermente **più alto**. Lo score
attuale non vede il problema: non protegge dai rapid loss, anzi i casi
peggiori (#46 score 8.0) hanno preso il punteggio massimo del periodo.

### 4. News e asset NON concentrano il problema

- **News**: n_news mediana 1.5 (rapid) vs 1.0 (developed). Differenza
  trascurabile, la presenza di news non spiega l'esito.
- **Asset/direzione** rapid loss: Brent short ×3, Gold long ×2, Gold
  short ×1, Nasdaq long ×1, Brent long ×1. **Distribuiti**, nessun
  cluster dominante. La sospetta concentrazione "Gold long" NON è
  confermata: solo 2 dei 4 Gold long sono rapid loss, e i rapid loss più
  pesanti sono Brent (#46 −2.52, #50 −2.98, #56 −3.31). Il problema sembra
  di regime, non di asset.

### 5. Timing: lieve skew a fine sessione (debole)

hour_utc mediana 15:00 (rapid) vs 12:00 (developed). I rapid loss pendono
verso il tardo pomeriggio UTC (sessione USA, ~17 IT), dove la volatilità
US è più alta. Coerente col pattern 1, ma con n piccolo è solo un indizio.

## Tabella per trade

| id | asset | dir | grp | pnl | peak_R | score | dpc | pct_dailyHigh | atr% | bb_w | hUTC |
|----|-------|-----|-----|-----|--------|-------|-----|---------------|------|------|------|
| 38 | Brent | short | RAPID | −0.88 | 0.28 | 7.0 | −5.05 | −5.28 | 1.95 | 5.63 | 16 |
| 39 | Nasdaq | long | RAPID | −0.79 | 0.38 | 7.5 | 1.44 | −0.24 | 0.89 | 1.92 | 19 |
| 41 | Gold | long | RAPID | −0.19 | 0.13 | 7.0 | −0.39 | −0.45 | 0.78 | 2.20 | 7 |
| 46 | Brent | short | RAPID | −2.52 | 0.45 | 8.0 | −3.38 | −3.51 | 1.81 | 11.42 | 9 |
| 47 | Gold | short | RAPID | −4.00 | −0.09 | 7.0 | −0.67 | −0.76 | 0.98 | 5.58 | 14 |
| 50 | Brent | short | RAPID | −2.98 | 0.11 | 7.0 | −0.80 | −1.12 | 1.75 | 6.94 | 20 |
| 53 | Gold | long | RAPID | −1.59 | 0.08 | 7.5 | 1.16 | −0.03 | 0.76 | 4.02 | 7 |
| 56 | Brent | long | RAPID | −3.31 | 0.05 | 7.5 | 2.07 | −1.05 | 1.65 | 7.97 | 19 |
| 40 | Nasdaq | long | DEV | 4.16 | 1.12 | 7.0 | 0.40 | −0.22 | 0.72 | 3.33 | 6 |
| 42 | Brent | short | DEV | 10.85 | 2.93 | 7.0 | 0.81 | −0.29 | 2.15 | 9.05 | 9 |
| 43 | Gold | long | DEV | −1.93 | 0.52 | 7.5 | 1.25 | −0.33 | 0.60 | 1.78 | 12 |
| 44 | Brent | long | DEV | −2.44 | 0.68 | 7.0 | 2.69 | −0.30 | 2.19 | 14.18 | 8 |
| 45 | Nasdaq | long | DEV | 0.00 | 1.27 | 7.5 | 0.28 | −0.36 | 0.49 | 2.55 | 19 |
| 48 | Nasdaq | long | DEV | 2.82 | 1.41 | 7.5 | 0.41 | −0.13 | 0.65 | 1.95 | 16 |
| 49 | Brent | short | DEV | 9.46 | 2.17 | 7.0 | −0.02 | −3.15 | 1.77 | 5.54 | 19 |
| 51 | Brent | long | DEV | −0.11 | 1.16 | 7.0 | 2.39 | −0.46 | 1.64 | 5.13 | 9 |
| 52 | Bitcoin | short | DEV | 2.01 | 2.08 | 7.0 | −1.92 | −2.45 | 0.70 | 2.52 | 12 |
| 54 | Nasdaq | long | DEV | −1.10 | 0.77 | 7.0 | 0.25 | 0.02 | 0.50 | 1.47 | 14 |
| 55 | Brent | long | DEV | −0.06 | 1.09 | 7.0 | 0.68 | −0.12 | 1.75 | 6.41 | 19 |
| 57 | Gold | long | DEV | −1.61 | 0.90 | 7.0 | 0.73 | −0.48 | 0.69 | 2.25 | 9 |
| 58 | Nasdaq | short | DEV | 6.77 | 2.10 | 7.0 | −0.88 | −0.97 | 0.66 | 2.89 | 12 |

dpc = daily_pct_change. Feature mancanti (rsi_14, trend_slope) omesse dalla
tabella per spazio: nei aggregati non discriminano (RSI mediana 48.4 vs
45.7; trend_slope_pct −0.061 vs −0.049, sovrapposti).

## Ipotesi da validare (NON azioni — per orientare Sprint 4)

1. **Filtro/penalità di volatilità all'entry**: cap o malus su atr_pct e
   bb_width elevati (soglia indicativa atr_pct > ~1.3 o bb_width > ~5). Da
   validare se separa davvero i rapid loss sui trade Sprint 3.
2. **Guard di posizione intraday**: entrare su asset già deboli nella
   giornata (daily_pct_change negativo / lontano dal massimo) sembra
   peggiore. Ipotesi: evitare/ridimensionare gli short che inseguono un
   ribasso già esteso. Da validare.
3. **Lo score non codifica il regime intraday**: dare al LLM (o a un
   pre-filtro deterministico) la volatilità e la posizione nel range
   giornaliero potrebbe migliorare la selezione. Da validare.
4. **Macro/sessione**: verificare con un calendario storico se i rapid
   loss si addensano vicino a eventi USA o nella sessione pomeridiana.
   Dimensione non coperta qui per mancanza del join calendario.

## Conclusione

Su questo sample i rapid loss non si distinguono per score, RSI, news o
asset, ma per **regime di mercato all'entry**: più volatili e su asset più
deboli nella giornata. Il segnale più solido è la volatilità (atr/bb ~2×).
Tutto preliminare e da riconfermare sui trade Sprint 3 con dati
`intra_trade_extreme` nativi prima di progettare qualunque intervento in
Sprint 4.
