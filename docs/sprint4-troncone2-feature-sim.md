# Sprint 4 troncone 2 — Fase 1: il segnale di regime è sfruttabile?

Data: 2026-06-09. Sola analisi offline su dati già in DB (feature all'entry
+ P&L realizzato), zero chiamate LLM, **nessuna modifica al sistema, monitor
invariato**. Replica l'approccio del trailing: simula prima di costruire.

Domanda: se il sistema avesse avuto un filtro/penalità sul regime intraday
(volatilità + posizione nella giornata), gli entry sarebbero migliorati? Il
segnale di Sprint 3.5 è sfruttabile o artefatto di sample piccolo?

Perimetro: Sprint 2, trade chiusi 38-58 = **21 trade, 6 WIN / 14 LOSS / 1 BE,
P&L baseline +12.56**. Sample piccolo: si leggono direzioni e robustezza, non
significatività.

## Sim 1 — Il segnale separa WIN da LOSS?

Distribuzione delle feature candidate (mediana WIN vs LOSS):

| feature | WIN (med) | LOSS (med) | separazione |
|---------|-----------|-----------|-------------|
| atr_pct_of_price | 0.71 | 1.31 | LOSS più volatili (come Sprint 3.5) |
| bb_width_pct | 3.11 | 5.35 | LOSS bande più larghe |
| pct_from_daily_high | −0.63 | −0.46 | sovrapposte (anzi WIN leggermente più in basso) |
| daily_pct_change | +0.19 | +0.71 | sovrapposte |

Le due feature di **volatilità** confermano la direzione di Sprint 3.5
(LOSS più volatili). Le due di **posizione nella giornata** invece **non
separano** WIN/LOSS (mediane sovrapposte): il segnale "posizione" di 3.5
era su rapid_loss-vs-developed, non regge su WIN/LOSS.

### Curva P&L-vs-soglia (volatilità, filtro "tieni se feature ≤ T")

atr_pct (punti chiave):

| soglia T | n_keep | P&L keep | Δ vs base | win_k | TP_k |
|----------|--------|----------|-----------|-------|------|
| 0.76 | 8 | 11.12 | −1.44 | 4 | **2** |
| 1.77 | 17 | 7.55 | −5.01 | 5 | 3 |
| 2.15 | 20 | 15.00 | **+2.44** | 6 | 4 |
| 2.19 (base) | 21 | 12.56 | 0 | 6 | 4 |

bb_width (punti chiave):

| soglia T | n_keep | P&L keep | Δ vs base | win_k | TP_k |
|----------|--------|----------|-----------|-------|------|
| 2.55 | 7 | −0.79 | −13.35 | 2 | 1 |
| 6.94 | 17 | 9.98 | −2.58 | 5 | 3 |
| 9.05 | 19 | 17.52 | **+4.96** | 6 | 4 |
| 14.18 (base) | 21 | 12.56 | 0 | 6 | 4 |

Lettura: per quasi ogni soglia che taglia davvero la volatilità alta il P&L
keep è **sotto il baseline**. Gli unici Δ positivi sono in cima: atr a T=2.15
(esclude solo il singolo trade più volatile, #44, un LOSS) e bb a T≈9 (esclude
i 2 trade a banda più larga, entrambi LOSS). Cioè "guadagni" solo escludendo
1-2 perdite all'estremo, non filtrando un regime.

## Sim 3 (il test critico) — Il filtro taglia i TP pieni?

Regime dei 4 TP pieni all'entry:

| id | asset | P&L | atr_pct | bb_width | regime |
|----|-------|-----|---------|----------|--------|
| 42 | Brent | +10.85 | **2.15** | **9.05** | **alta volatilità** |
| 49 | Brent | +9.46 | **1.77** | **5.54** | **alta volatilità** |
| 52 | Bitcoin | +2.01 | 0.70 | 2.52 | bassa |
| 58 | Nasdaq | +6.77 | 0.66 | 2.89 | bassa |

**Questo è il risultato che affonda il filtro di volatilità.** I 4 TP sono
**spaccati**: i due più grandi (#42 +10.85, #49 +9.46) sono in **alta
volatilità**, esattamente dove stanno anche i rapid loss. atr_pct dei WIN
arriva a 2.15, dei LOSS a 2.19: **si sovrappongono al centesimo**.

Conseguenza diretta: un filtro che taglia la volatilità alta per eliminare i
loss **taglia anche #42 e #49**. Sulla curva atr_pct, qualunque soglia ≤ 1.77
butta via #49 (e ≤2.15 anche #42): a T=0.76 restano 2 TP su 4 e il P&L scende
a 11.12 < 12.56. Si perde più (~+20 di TP) di quanto si risparmia sui loss.

## Sim 2 — Sensibilità alla soglia (overfitting)

I (rari) Δ positivi vivono in **finestre strettissime** e spariscono/si
invertono spostando la soglia di poco:

- bb_width: +4.96 esiste **solo** tra T≈8 e T≈11, perché la soglia deve
  infilarsi tra il TP #42 (bb 9.05) e i due loss (#46 a 11.42, #44 a 14.18).
  Abbassa a 8 → tagli anche #42 (−10.85); alza a 12 → ritieni #46 (un loss).
- atr_pct: +2.44 solo a T=2.15 = "tieni tutto tranne il singolo peggiore"
  (degenerato, non un filtro).
- pct_daily_high: +3.40 a T=−3.15 ma a −2.45 crolla a −6.06 perché tagli il
  TP #49 (a −3.15). Stessa lama.

In tutti i casi il "beneficio" dipende dal fatto che un TP siede **adiacente**
a un loss nello spazio delle feature, e la soglia deve separarli a mano. Su 21
trade è **overfitting puro**: nessun beneficio robusto a piccole variazioni.

## Sim 4 — Via A (filtro deterministico) vs Via B (feature all'LLM)

Premesso che l'evidenza offline **non conferma** un segnale sfruttabile:

- **Via A (filtro/penalità a valle, stile guardrail)**: trasparente,
  testabile, reversibile. MA l'analisi mostra che una soglia fissa o non aiuta
  o overfitta, e soprattutto **non sa distinguere** l'alta-vol-buona (breakout
  Brent short → TP) dall'alta-vol-cattiva (chop che fallisce): le tagliano
  entrambe. Per un segnale confuso, il filtro hard è lo strumento sbagliato.
- **Via B (regime come feature nel prompt LLM)**: l'unico modo per pesare il
  regime **in contesto** (l'LLM potrebbe imparare che Brent short in alta vol
  con trend_slope negativo è diverso da Gold long in alta vol su down-day). MA
  Sim C ha già mostrato che lo score non usa bene il regime, e dare una feature
  in più non garantisce che la sfrutti; va validato, non è gratis.

Conclusione preliminare: se mai si procedesse, **solo Via B** (contestuale),
non Via A (hard filter). Ma l'evidenza offline non basta a giustificare
nemmeno Via B adesso.

## Raccomandazione

**Il segnale di regime NON è robustamente sfruttabile su questo sample.** Tre
ragioni convergenti:
1. Solo la volatilità separa (debolmente) WIN/LOSS; la posizione nella
   giornata no. 
2. **I due TP più grandi sono in alta volatilità**, gli stessi valori dei
   rapid loss: un filtro di volatilità li taglia insieme alle perdite.
3. I (pochi) guadagni apparenti sono overfitting in finestre di soglia
   strettissime, instabili a piccole variazioni su 21 trade.

È un **risultato negativo utile**, come la Sim C per il fix scoring: ci
risparmia di costruire un filtro di regime che taglierebbe i TP coi loss.

Cosa fare: **non costruire il filtro di regime ora.** Opzioni:
- riprovare questa analisi quando il sample cresce (più WIN, più TP, per
  vedere se i TP smettono di vivere in alta volatilità o se è strutturale —
  i TP grandi sono Brent short, e Brent È volatile: probabilmente strutturale);
- se si vuole comunque attaccare la qualità entry, cercare un segnale
  **diverso** da volatilità/posizione (es. allineamento delle due pendenze,
  contesto news/catalyst, o l'interazione asset×regime), non quello di 3.5.

## Limiti

Sample 21 trade (6 WIN / 14 LOSS / 1 BE): direzioni, non significatività.
Feature storiche ricostruite. P&L realizzato influenzato anche da trailing e
chiusure manuali, non solo dall'entry. WIN/LOSS sul segno del P&L (non
rapid/developed di 3.5, scelta apposta per la domanda "migliora il P&L?").
Da riconfermare su sample più ampio.
