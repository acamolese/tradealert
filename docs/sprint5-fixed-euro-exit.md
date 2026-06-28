# Sprint 5 — Uscita a soglia fissa in euro (+1€ / −1€): analisi (pre-registrata)

Data: 2026-06-28. Analisi offline, sola lettura. Testa una leva di GESTIONE
alternativa al trailing: cosa sarebbe successo uscendo da ogni trade alla prima
volta che il P&L non realizzato tocca **+1€** (take profit fisso) o **−1€**
(stop fisso), invece dell'attuale gestione (trailing D/V1/V2 + chiusure
manuali/auto-close). Pre-registrazione scritta PRIMA di calcolare gli esiti.

## Contesto

Domanda dell'utente: come si sarebbe comportata la strategia uscendo a soglia
monetaria fissa ±1€. È un bracket molto stretto: il |pnl| mediano reale è 1.63€,
quindi ±1€ sta sotto la mezza giornata tipica di un trade. La strategia oggi ha
una distribuzione a coda destra grassa: il totale reale (+12.61€ su 74 trade,
media +0.17€, mediana −0.46€) è retto da pochi TP grandi (#42 +10.85, #49 +9.46,
#65 +10.26, #81 +7.21, #58 +6.77). Un cap a +1€ amputa quella coda; uno stop a
−1€ salva i loss grandi (#88 −5.65, #68/#35 −5.05, #78 −4.38). Il path conta:
un target a +1€ è vicino all'entry, molti trade finiti in piccola perdita
potrebbero averlo toccato prima di girare (loser→winner), e viceversa piccoli
winner potrebbero aver toccato −1€ prima (winner→loser). Solo la ricostruzione
del cammino lo dice.

## Metodo

Riusa il motore di `jobs/trailing_calibration.py`: ricostruisce la traiettoria
del trade dalle candele Capital 5m (fallback 15m/30m) sulla vita reale
opened→closed, walk candela per candela.

- **Campione:** trade chiusi con `pnl` reale non nullo, asset mappabile a un
  epic Capital, e ≥3 candele ricostruibili nella finestra opened→closed. Si
  riportano copertura e scartati. (I primi ~18 trade alt-coin/azioni pre-strategia
  hanno pnl NA → fuori.)
- **Conversione quote→EUR:** il P&L intermedio va espresso in euro. Per ogni
  trade si stima un fattore `conv = pnl_reale / (escursione_quote_alla_chiusura ·
  size)`, dove l'escursione di chiusura è (entry − close_ask) per gli short e
  (close_bid − entry) per i long sull'ultima candela. Così a fine vita
  l'escursione in euro coincide con `pnl` reale per costruzione. Per i trade con
  pnl≈0 (conv instabile) si usa la mediana dei conv validi dello stesso asset
  (fallback: mediana del gruppo USD).
- **Simulazione bracket ±X€:** a ogni candela si calcola l'escursione favorevole
  in euro (su low-ask short / high-bid long) e quella avversa (su high-ask short /
  low-bid long). Regola di uscita, first-touch:
  - se nella stessa candela avversa ≤ −X **e** favorevole ≥ +Y → **stop −X**
    (conservativo: SL prima di TP, come nelle altre sim);
  - elif favorevole ≥ +Y → **+Y**;
  - elif avversa ≤ −X → **−X**;
  - altrimenti prosegui. Se nessun trigger entro la vita del trade → uscita al
    close dell'ultima candela (= pnl reale per costruzione).
- **Confronto:** somma EUR del bracket vs somma `pnl` reale, sugli stessi trade.
  Metriche: totale EUR, media/trade (expectancy), win rate. Headline: +1€/−1€;
  per contesto anche una piccola griglia (target ∈ {1,2,3}, stop ∈ {1,2,3}).

## Pre-registrazione (scritta PRIMA dei risultati)

**Predizione (mia, prima di guardare):** il bracket ±1€ **abbassa il totale**
rispetto al reale. La win rate **sale** (un target a +1€ è facile da toccare, molti
loser oscillanti escono verdi), ma l'expectancy/totale **scende** perché l'edge
della strategia è tutto nella coda destra (i 4-5 TP grandi che da soli fanno il
+12.61€): cappare quei winner a +1€ rimuove più di quanto lo stop a −1€ salvi sui
loss grandi. È il pattern classico "taglia corti i winner". Magnitudo attesa: il
totale crolla ben sotto +12.61€, plausibilmente verso lo zero o negativo.

**Criterio di decisione (pre-fissato):**
- bracket ±1€ totale **> reale + 5€** → sorpresa, vale approfondire come leva di
  gestione (pre-registrare un gate forward come per V1).
- bracket ±1€ totale **< reale** → conferma "taglia corti i winner": NEGATIVO,
  archiviare, non implementare.
- in mezzo (reale .. reale+5€) → neutro, non vale il rischio di cappare la coda.

Metrica primaria = totale EUR. Win rate è secondaria (può salire mentre il totale
scende: è proprio il fallimento atteso). Niente HARKing: se dalla griglia emerge
un bracket "migliore", si pre-registra a parte, non si rivendica qui.

## Esiti (2026-06-28, `jobs/fixed_euro_exit.py`)

Copertura piena: 70 trade candidati, 70 simulati, 0 scartati (candele Capital
disponibili su tutta la vita reale). Conv quote→EUR per-asset stimati coerenti con
gli FX attesi (Gold 0.983, Brent 1.030, Nasdaq 1.001, US500 0.868, Bitcoin 1.042,
Hang Seng 0.123 ≈ HKD→EUR).

**Headline +1€ / −1€ (n=70):**

| | totale | media/trade | win rate |
|---|--------|-------------|----------|
| REALE | **+14.27€** | +0.204€ | 23/70 (33%) |
| BRACKET ±1€ | **+5.86€** | +0.084€ | 38/70 (54%) |
| delta | **−8.41€** | | |

**La predizione è confermata in pieno, anche nella firma del fallimento.** La win
rate **sale** (33%→54%: un target a +1€ è facile da toccare) ma il totale **scende**
di 8.41€ (−59%). È il pattern "taglia corti i winner": l'edge della strategia è
nella coda destra e il bracket la amputa.

**Robustezza all'assunzione intra-candela:** SL-first (conservativo) e TP-first
(ottimista) danno lo **stesso identico** +5.86€ → nessuna candela 5m tocca
entrambe le soglie nello stesso intervallo, l'ordinamento non incide. Il verdetto
non dipende dall'assunzione.

**Meccanismo (dalla tabella per-trade):**
- Coppa i winner sopravvissuti a +1€: #42 (+10.85→+1), #65 (+10.26→+1),
  #81 (+7.21→+1), #58 (+6.77→+1), #25/#19 (+6.3/+6.1→+1), #79 (+6.11→+1),
  #40 (+4.16→+1).
- Peggio: stoppa a −1€ winner che PRIMA hanno ritracciato sotto −1€ e poi sono
  esplosi: #49 (+9.46→−1), #34 (+5.83→−1), #84 (+4.84→−1). Il bracket non li vede
  mai diventare winner.
- In compenso salva i loss grandi (#68 −5.05→+1, #88 −5.65→−1, #78 −4.38→−1,
  #47 −4.0→−1) e converte molti loser oscillanti in +1 (#29, #44, #57, #80…).
- Il saldo: l'upside perso supera il downside salvato.

**Griglia (totale EUR, riga = stop −X, col = target +Y):**

| −X \ +Y | 1 | 2 | 3 |
|---------|------|------|------|
| 1 | 5.86 | 1.15 | 4.16 |
| 2 | −0.67 | −1.21 | 4.12 |
| 3 | 6.83 | 3.31 | **10.32** |

**Nessuna cella batte il reale (+14.27€).** La migliore (−3/+3 = 10.32€) resta
sotto, e i target stretti (+1/+2) sono i peggiori: più si cappa, peggio è. La
superficie è rumorosa (n piccolo, path-dependent), ma il segno è univoco.

## Decisione

Criterio pre-fissato: bracket ±1€ (5.86€) **< reale** (14.27€) → **NEGATIVO,
archiviare, non implementare.** L'uscita a soglia monetaria fissa taglia corti i
winner e distrugge l'edge a coda destra; la win rate più alta è un'illusione
(meno expectancy). Coerente col pattern meta del progetto (i pochi TP grandi
reggono tutto) e col fatto che la leva di gestione vincente finora è il trailing
V1, che protegge SENZA cappare la coda (delta-trend = 0). Quinto risultato di
selezione/uscita rigida negativo; la gestione efficace resta adattiva (trailing),
non a soglia fissa.

## Limiti

Conversione quote→EUR stimata (non l'FX tick-by-tick); entry/exit al close
candela, non al tick; first-touch su candele 5m senza spread/slippage (un bracket
stretto nella realtà verrebbe toccato dal rumore più spesso → bias ottimista sul
numero di trigger); finestra = vita reale del trade (le chiusure manuali reali
sono sostituite dalla regola bracket); sample limitato ai trade con candele
storiche disponibili. Direzioni, non significatività statistica.
