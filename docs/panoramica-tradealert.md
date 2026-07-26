# TradeAlert — Panoramica del sistema

*Documento di sintesi. Ultimo aggiornamento: 2026-07-26.*

TradeAlert è un sistema automatico di trading su CFD (broker Capital.com) che
scansiona un paniere di asset, seleziona il "setup" migliore secondo una regola
deterministica, apre la posizione, ne gestisce l'uscita con stop dinamici più un
monitor LLM, e comunica tutto via Telegram. Gira H24 su una VM, con un conto reale
di piccola taglia (~60 €) usato come laboratorio per validare o falsificare ipotesi
di trading con metodo, prima di rischiare capitale maggiore.

Il valore del progetto non è (ancora) un edge di mercato dimostrato: è
un'**infrastruttura di ricerca disciplinata** che ha testato e archiviato molte
ipotesi con rigore, distinguendo ciò che funziona da ciò che è illusione statistica.

---

## 1. Come funziona, end-to-end

Ogni ora (07-22, ora italiana) parte uno **scan**:

1. **Raccolta dati** — per ogni asset del paniere si scaricano candele e snapshot
   da Capital (prezzo, spread, ATR, variazione del giorno `dpc`).
2. **Pre-filtro** — si scartano gli asset non tradabili, con spread troppo largo,
   troppo vicini ai massimi, in compressione, ecc.
3. **Selezione (v1-momentum, deterministica)** — si sceglie il "mover più forte del
   giorno": l'asset con il maggior |dpc| sopra soglia. Direzione = segno del
   momentum (continuazione). Stop = max(1.5×ATR%, 0.5%), target = 2×stop. Nessuna
   chiamata a un LLM in ingresso (scelta di luglio 2026: zero costo, stessa resa).
4. **Sizing** — la size è calcolata per bloccare al massimo il budget di margine
   (20 €), con un cap di perdita per trade (6 €), usando la **leva reale** dello
   strumento e la conversione valuta corretta.
5. **Apertura** — con `execution_mode=confirm` la proposta arriva su Telegram con
   una finestra di veto (auto-conferma se nessuno interviene). La posizione viene
   aperta su Capital con stop e take-profit server-side.
6. **Gestione dell'uscita** — due meccanismi in parallelo:
   - **Trailing stop** (ogni 5 min, deterministico, gratis): sposta lo stop verso
     il profitto secondo la regola "D + V1 + V2".
   - **Monitor LLM** (ogni 30 min): valuta se chiudere in anticipo; con finestra di
     veto auto-chiude i trade che "girano". È il componente che aggiunge più valore.
7. **Chiusura e riconciliazione** — quando lo stop/target scatta o il monitor
   chiude, un job di `reconcile` (ogni ora) allinea lo stato del database al conto
   reale, registrando prezzo di chiusura e P&L.

Un'**agenda** (ogni giorno alle 08:20) sorveglia i "gate" pre-registrati e avvisa
su Telegram quando un esperimento raggiunge il campione previsto per essere valutato.

---

## 2. Stack e infrastruttura

| Componente | Ruolo |
|---|---|
| **VM Oracle** (Ubuntu) | Esegue tutti i cron e il listener Telegram H24 |
| **Capital.com** (API) | Broker CFD: prezzi, ordini, posizioni, storico transazioni |
| **Supabase** (Postgres) | Database: signal, trade, eventi di monitoraggio, scan |
| **Telegram** | Interfaccia utente: proposte, conferme, avvisi, comandi (`/posizioni`, `/status`) |
| **Anthropic (Claude)** | LLM per il monitor delle uscite (l'ingresso è deterministico) |

**Cron principali** (TZ Europe/Rome): `morning_scan` (orario 07-22), `monitor`
(30 min, 07-22), `trailing_stop` (5 min, H24), `reconcile` (orario), `intra_trade_log`
(30 min, logging MFE), `open_rate_check` (22:35), `health_check` (09:10),
`weekly_report` (domenica 20:00), `cost_report` (lunedì 08:00), `sprint6_agenda`
(08:20), `macro_scan` (06:50).

---

## 3. Configurazione attualmente in produzione (2026-07-26)

**Paniere: solo INDICI** — US500, Nasdaq 100 (US100), Germany 40/DAX (DE40),
Wall Street 30/Dow (US30), Gold. Ridisegnato il 2026-07-24 sulla base dei test
(vedi §5). Brent, Bitcoin, Copper, FX e asiatici sono stati rimossi dallo scan
(restano "conosciuti" per gestire eventuali posizioni residue).

**Parametri chiave:**

| Parametro | Valore | Significato |
|---|---|---|
| `MIN_SCORE_THRESHOLD` | 7.2 | Entra solo se |dpc| ≥ ~2% (score = 7.0 + |dpc|/10) |
| `EXPOSURE_BUDGET_EUR` | 20 | Margine massimo bloccato per trade |
| `MAX_LOSS_PER_TRADE_PCT` | 6 | Cap di perdita per trade: 6 € |
| `MAX_OPEN_POSITIONS` | 3 | Posizioni aperte contemporanee |
| `WEEKLY_DRAWDOWN_CAP` | 20 € | Se la perdita 7gg supera 20 €, lo scanner si ferma |
| `ACCOUNT_RISK_CAPITAL_EUR` | 100 | Capitale di rischio nominale (leva dei cap) |

**Flag attivi:** `SCORING_LLM_OFF=true` (ingresso deterministico), `TRAIL_V1_LOWBAND`
+ `TRAIL_V2_HIGHBAND` (trailing calibrato), `AUTO_CLOSE_ENABLED` (monitor auto-chiude),
`REAL_LEVERAGE_SIZING` (leva reale per-strumento), `SIZING_CURRENCY_AWARE`
(conversione valuta), `CONCENTRATION_BLOCK_DUP` (no doppioni asset+direzione).
`BASKET_FX_ENABLED` e `BASKET_TREND_ENABLED` a false (paniere ristretto agli indici).

---

## 4. Il metodo: perché ci si può fidare dei risultati

Ogni ipotesi segue lo stesso protocollo, che è ciò che distingue TradeAlert da un
sistema "provato a occhio":

1. **Pre-registrazione** — prima di guardare i numeri si scrive un documento con
   la domanda, la metrica, e le **soglie di decisione (gate)**. Il file viene
   committato: il timestamp è la garanzia contro l'aggiustamento a posteriori (HARKing).
2. **Normalizzazione in R** — gli esiti si misurano in multipli del rischio (R),
   non in euro, così i risultati non dipendono dalla size e sono robusti al bug
   dei P&L in euro (vedi §7).
3. **Robustezza** — un verdetto che si rovescia togliendo 1-2 trade non è un
   verdetto (lezione dei trade #42/#49). Si controlla sempre "senza i migliori/peggiori".
4. **In-sample / out-of-sample** — sul backtest lungo (candele 2020-2026) si sceglie
   su un periodo e si giudica su un altro, per non adattarsi al rumore.
5. **Un esperimento alla volta** in produzione, con flag reversibili e rollback
   documentato.

---

## 5. I test fatti, con gli esiti

La tabella riassume; sotto, i punti che contano di più.

| Ambito | Ipotesi | Esito |
|---|---|---|
| **Selezione — score LLM** | lo score dell'LLM predice l'esito | **NO** (correlazione ≈ 0) → tolto in ingresso |
| **Selezione — volatilità/regime** | filtrare per regime di volatilità migliora | **NO** (taglia i vincitori grandi) |
| **Selezione — chasing** | l'estensione del movimento all'entrata predice l'esito | **NO** (segnale nullo) |
| **Selezione — timing/frequenza ingresso** | scannerizzare più spesso fa entrare meglio | **NO** (piatto) |
| **Gestione — frequenza trailing** | aggiornare lo stop più spesso migliora | **NO** (piatto, semmai peggio) |
| **Gestione — calibrazione trailing** | un trail più stretto batte l'opzione D | **Artefatto** (over-optimism di granularità) |
| **Gestione — monitor LLM** | il monitor distrugge valore tagliando i runner | **NO, è FALSO**: non distrugge, aiuta |
| **Gestione — hard-exit a metà stop** | uscire a -0.5R fisso migliora | **NO** (lo fa già il monitor) |
| **Gestione — uscita a euro fisso** | bracket +1/-1 € migliora | **NO** (taglia corti i vincitori) |
| **Rischio — gap di weekend** | gli asiatici gappano più degli altri | **NO**: è il Brent il più gap-prone |
| **Rischio — tenere nel weekend** | il weekend ha un edge/danno sistematico | **Solo rischio-coda**, nessun edge |
| **Direzione — long** | il lato long va limitato | **Non conclusivo** (bleed normalizzato) |
| **Motore deterministico (M4)** | una strategia trend-following batte il sistema | **FALSIFICATO** (walk-forward, 3 famiglie bocciate) |
| **Paniere — quali asset** | il momentum ha edge su tutti | **NO**: solo sugli **indici azionari maggiori** |
| **Paniere — allargare** | più asset = più opportunità | **NO** (diluisce, il selettore pesca i perdenti) |

### I tre risultati che orientano tutto

1. **L'ingresso non ha edge.** Score, volatilità, chasing, regime, timing, frequenza:
   ogni leva sulla *selezione* del trade è risultata nulla o negativa. Da qui la
   scelta di togliere l'LLM in ingresso (correlazione ≈ 0 con gli esiti): stesso
   risultato, costo azzerato. Il selettore è ora una semplice regola di momentum.

2. **La gestione dell'uscita è dove c'è valore, ma nel COME, non nel quanto spesso.**
   Il **monitor LLM** taglia i loser prima dello stop pieno e migliora l'esito
   realizzato (verificato: nella settimana peggiore ha trasformato -2.6R di stop in
   +2.8R di chiusure gestite). La *frequenza* dei controlli invece è irrilevante, e
   la "calibrazione miracolosa" del trailing vista su pochi trade si è rivelata un
   artefatto della granularità oraria del backtest (uno stop strettissimo sembra
   ottimo solo se la simulazione non vede il rumore che lo colpirebbe).

3. **Il momentum funziona sugli indici, non altrove.** Misurando l'expectancy per
   asset su 6 anni: US500, Nasdaq, DAX, Dow, Gold hanno edge positivo e robusto
   anche out-of-sample; commodity (Brent, Copper), crypto e FX perdono. Da qui il
   ridisegno del paniere ai soli indici + soglia 7.2, l'unica configurazione con
   expectancy positiva nel backtest (+0.052R, +0.118 out-of-sample). **In
   validazione forward** (gate a 25 trade, vedi §8).

---

## 6. Cosa NON fare (lezioni archiviate, da non ri-esplorare)

- Non reintrodurre un LLM decisionale in ingresso: il mercato prezza già ciò che
  saprebbe leggere.
- Non aumentare la frequenza di scan o di trailing: falsificato in ingresso e uscita.
- Non allargare il paniere per "avere più occasioni": diluisce.
- Non fidarsi di uno stop stretto che vince nel backtest orario: serve validazione
  su dati tick.
- Non costruire filtri di selezione basati su volatilità/regime/estensione: quattro
  test negativi indipendenti.

---

## 7. Bug e caveat noti

- **⚠️ P&L in euro nel database inaffidabili (aperto).** Per un errore di
  conversione valuta/segno, i P&L in euro registrati nel DB divergono dal conto
  reale (reale ≈ +28 € da trading contro -6 € nel DB su tutta la storia). **Tutte
  le analisi si fanno in R**, non in euro. Il weekly report in euro va letto con
  cautela finché non si ri-sincronizzano i valori storici dalle transazioni Capital.
- **Sizing a leva reale (fixato 2026-07-21).** Prima il sistema stimava il margine
  con la leva di categoria (20) invece di quella reale per-strumento (Brent 10):
  i setup "da 40 €" ne bloccavano ~70 e Capital rifiutava gli ordini. Corretto con
  una mappa di leva reale + auto-calibrazione dal dato di posizione.
- **Conversione valuta nel pre-filtro e nel reconcile (fixato).** Nikkei (JPY) e
  Hang Seng (HKD) davano margini e P&L in valuta estera non convertita.
- **Conto reale solo dalla VM.** L'ambiente locale punta a un conto demo diverso;
  il conto operativo (~60 €) si legge solo interrogando Capital dalla VM.

---

## 8. Stato attuale, potenzialità e limiti

**Numeri onesti.** L'expectancy del sistema è vicina a zero: alterna settimane
positive e negative dentro una varianza di ±1.5R. Il conto è piccolo (~60 €), quindi
le oscillazioni in euro sono contenute ma emotivamente amplificate. Dopo una
settimana negativa (-21 € reali, quasi tutta su due stop asiatici) il rischio è
stato ridotto (budget 20 €, cap 6 €) e il paniere concentrato sugli indici.

**La scommessa in corso.** Il ridisegno agli indici è l'unica configurazione che i
dati indicano con edge positivo. È in **validazione forward pre-registrata**: a 25
trade indici chiusi, se l'expectancy realizzata è ≥ +0.05R (robusta) il ridisegno è
confermato; se ≤ -0.05R si torna indietro (o si riconsidera Bitcoin, che nel backtest
è pessimo ma nei trade reali era il migliore — segno che il monitor lo gestisce bene).

**Le potenzialità concrete:**
- Se il paniere indici regge forward, si può **rialzare gradualmente il sizing** e,
  superato il gate di scaling (50 trade con exp ≥ +0.15R, profit factor ≥ 1.3),
  aumentare il capitale di rischio.
- Il **monitor LLM** è la leva meno esplorata e l'unica che crea valore: c'è spazio
  per migliorarne le decisioni di uscita (non la frequenza).
- L'infrastruttura di backtest deterministico permette di testare qualunque nuova
  ipotesi su 6 anni di dati in secondi: il costo marginale di una nuova domanda ben
  posta è bassissimo.

**I limiti da tenere presenti:**
- Nessun edge di *selezione* è stato trovato: il sistema vive o muore sulla gestione
  e sulla scelta del paniere.
- Il paniere indici **non opera nel weekend** (mercati chiusi): scelta coerente
  (il weekend era solo rischio-coda), ma riduce il numero di occasioni.
- Il campione è piccolo e la varianza alta: ogni verdetto va preso come "protezione
  dal disastro", non come certezza.

---

## 9. Dove guardare nel repository

- `src/scanner.py` — pipeline di scan e selezione.
- `src/entry_shadow.py` — la regola deterministica v1-momentum.
- `src/position_monitor.py` — trailing (D+V1+V2) e monitor LLM.
- `src/risk.py`, `src/leverage.py` — sizing, leva reale, conversione valuta.
- `jobs/backtest_run.py` — motore di backtest lungo riusabile.
- `docs/sprint*.md` — le pre-registrazioni e gli esiti di ogni esperimento.
- `docs/sprint8-indici-gate.md` — il gate forward attualmente aperto.
