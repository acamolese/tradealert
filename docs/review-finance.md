# TradeAlert, review trading/finance

Data review: 2026-04-25
Perimetro: codice in `/Users/andreacamolese/tradealert/src` e config in `/Users/andreacamolese/tradealert/config`. Ho letto direttamente scanner, llm_analyzer, features, risk, executor, position_monitor, reconcile, macro_guard, performance, market_context, discovery, universe, watchlist, quiet_hours, config.

## 1. Sintesi

Il sistema ha un'architettura corretta per fase Coach (segnalazione, sizing su margine, trailing R-multiple, reconcile, dedup giornaliero, guardrail macro post-LLM). È una pipeline ben curata di plumbing, non una strategia con edge dimostrato. I problemi principali sono di natura statistica e di mismatch fra prompt e feature: l'LLM riceve un set di feature povero (RSI 14, ATR, slope 20, BB width, pct_from_high_20, daily_pct_change) ma il prompt finge che riceva EMA20/50/200, BB upper/middle/lower, supporti e resistenze, livelli chiave. Il modello, di conseguenza, produce thesis che citano livelli che non ha mai visto, generando overconfidence sistematica. La mancanza di un edge tracciabile (no logging delle feature nel signal, no backtest harness, sample troppo piccolo per concludere alcunché) impedisce di capire se i loss sono casualità o difetto strutturale. I rischi finanziari maggiori sono: assenza di un cap di drawdown giornaliero/settimanale, esposizione weekend con stop server-side che NON protegge dai gap (CFD su crypto sì, ma su indici/commodities tradizionali sabato/domenica i mercati sono chiusi e il gap di apertura può saltare lo stop), nessun filtro di regime di mercato e nessun controllo di correlazione strutturale (solo macro_group binario risk_on/off). L'edge potenziale da preservare sono: il pre-filtro deterministico, il blow-off filter weekend, l'allowlist crypto major, il dedup 24h, il trailing half-risk a 0.5R che è già un buon comportamento.

## 2. Findings prioritizzati

### P0 - Critici per perdita capitale

#### P0.1 Mismatch fra system prompt LLM e feature realmente passate
- File: `src/llm_analyzer.py:62-173` vs `src/features.py:84-158`
- Il system prompt parla di `slope EMA20/50 4H allineato alla direzione`, `bollinger band direzionali (banda nel verso del trend)`, `breakout 2100 con volume`, `RSI 4H esce da ipervenduto`, `rottura resistenza 200EMA`, `level_support`, `level_resistance`. `compute_features` produce SOLO: `last_price`, `rsi_14`, `trend_slope_pct` (su 20 candele, non EMA20/50), `atr_4h`, `atr_pct_of_price`, `bb_width_pct` (solo l'ampiezza, non upper/middle/lower), `high_20`, `low_20`, `pct_from_high_20`, `spread_pct`, `market_status`, `daily_pct_change`, `daily_range_pct`, `pct_from_daily_high`, `min_size`, `size_step`, `margin_factor`. La compressione in `_compress_features` arrotonda chiavi `level_support`, `level_resistance`, `bb_upper/middle/lower`, `ema_20/50/200` che NON ESISTONO nel payload.
- Impatto P&L: l'LLM costruisce thesis su livelli e indicatori inventati. Ogni "rottura della 50EMA" o "appoggio sulla banda inferiore" è confabulato. Lo score è quindi rumoroso e l'utente legge una motivazione falsamente quantitativa, perdendo capacità critica.
- Raccomandazione: due opzioni concrete.
  - Opzione A (migliore): aggiungere a `compute_features` calcolo reale di EMA20/50/200, BB upper/middle/lower, livelli swing-high/swing-low pivot recenti (max/min locali su finestra 30-50 candele), volume relativo (volume corrente / SMA20 volume). Solo allora il prompt è coerente con il payload.
  - Opzione B (rapida): allineare il prompt al payload effettivo. Eliminare ogni riferimento a EMA, BB upper/lower, supporti/resistenze; mantenere SOLO le feature che esistono. È meno potente ma elimina il bias.

#### P0.2 Esposizione weekend con stop server-side e rischio di gap su CFD non-crypto
- File: `src/scanner.py:872-902` (allowlist weekend), `src/quiet_hours.py:25-29` (sabato e domenica monitor attivo), `src/executor.py` (stop server-side)
- Sabato e domenica vengono filtrati gli asset al solo gruppo crypto major: corretto. Ma i trade aperti il venerdì su indici/commodities/forex restano aperti tutto il weekend e lo stop server-side è teorico: l'apertura del lunedì può gappare oltre lo stop. Capital potrebbe (nel contratto) chiudere a "best available", non al prezzo di stop. Non vedo nessun controllo di "chiudere posizioni non-crypto entro venerdì 21:00".
- Impatto P&L: un singolo gap weekend può portare la perdita reale a 2-5x il rischio teorico, in particolare su DAX 40, US500, Nasdaq 100 dopo notizie del weekend.
- Raccomandazione: aggiungere job venerdì 21:30 (Europe/Rome) che chiude o riduce le posizioni non-crypto, oppure rifiuta di aprirle dopo le 18:00 di venerdì. Documentare esplicitamente il "weekend gap risk" in ogni messaggio Telegram aperto venerdì pomeriggio.

#### P0.3 Nessun cap di drawdown giornaliero, settimanale, mensile
- File: `src/config.py:11-39`, `src/risk.py:43-99`, `src/executor.py:113-173`
- Il rischio massimo per trade è limitato implicitamente dal `exposure_budget_eur` (margine bloccato) e dallo `stop_pct` del signal, ma non c'è alcun "hard cap" che dica "se hai perso X EUR oggi, smetti di aprire trade fino a domani" o "se il drawdown settimanale supera Y%, ferma il sistema". `MAX_OPEN_POSITIONS` (default 1) è l'unico vincolo.
- Impatto P&L: una serie sfortunata di 5 stop consecutivi su un account piccolo è una perdita del 30-50% non monitorata. L'analisi weekly del 2026-04-25 (5 chiusi 0 win, -2.19 EUR) dice esattamente questo: il sistema non ha self-preservation.
- Raccomandazione: introdurre `MAX_DAILY_LOSS_EUR`, `MAX_WEEKLY_LOSS_EUR`, `MAX_OPEN_RISK_EUR` (somma dei rischi residui di tutte le posizioni aperte). Lo scanner all'inizio della run interroga il DB per i pnl di oggi/settimana e se sotto soglia salta direttamente con notifica "circuit breaker attivo". Per fase Coach basta che lo scanner NON proponga setup; in fase auto deve impedire l'esecuzione.

#### P0.4 Sample size insufficiente per qualsiasi conclusione statistica
- File: `src/performance.py:32-48` (bucket score), `src/performance.py:222-266` (suggest_*)
- I bucket di score sono granulari 0.5 e la soglia `MIN_RELIABLE_SAMPLE = 3` è generosa. Una conclusione "Score >= 8 funziona" su 6 trade non è un edge, è rumore. La memoria progetto cita 5 trade chiusi 0 win nel weekend: l'inferenza "alt-coin micro-cap perdono" su 5 trade è plausibile come narrativa ma statisticamente non è altro che un campione vincolato.
- Impatto P&L: si rischia di tunare le soglie sul rumore (overfit retrospettivo) e perdere edge reali, oppure di sviluppare false convinzioni su pattern.
- Raccomandazione: prima di ogni decisione di tuning, requisito minimo 30 trade (idealmente 100) per partizione confrontata. Nel frattempo: trattare ogni "scoperta" come ipotesi non confermata, mai come regola hard. Etichettare i suggerimenti automatici di `performance.py` con un flag "tentativo, n<30".

### P1 - Alto

#### P1.1 Stop e target percentuali generati dall'LLM senza ancoraggio ad ATR
- File: `src/llm_analyzer.py:120-121`, `src/scanner.py:224-236` (calcolo SL/TP da pct), `src/executor.py:176-194`
- Il prompt dice "Per crypto usa stop più larghi (3-5%) per gestire la volatilità tipica" ma non vincola lo stop all'ATR misurato dell'asset. Risultato: stop 1.5% su asset con ATR_4h del 3% è inevitabilmente colpito dal rumore; stop 5% su asset con ATR_4h dello 0.5% è pigro e brucia capitale.
- Impatto P&L: stop loss colpiti per rumore (false stop-out) o profit lasciati troppo lontani.
- Raccomandazione: in Python, dopo aver ricevuto i pct dall'LLM, riscalarli rispetto ad ATR. Esempio: `effective_stop_pct = max(llm_stop_pct, 1.5 * atr_pct_of_price)` e `effective_target_pct = max(llm_target_pct, 2.0 * effective_stop_pct)` per garantire R:R minimo. È deterministico e indipendente dal modello.

#### P1.2 RSI calcolato con metodo SMA invece che Wilder smoothing
- File: `src/features.py:37-48`
- `_rsi` usa `gains[-period:].mean()` e `losses[-period:].mean()`: è una SMA dei gain/loss, non l'RSI standard di Wilder che usa exponential smoothing. Le soglie 30/70 nel pre-filtro e nel prompt assumono RSI Wilder. La differenza è piccola in trend forti, sensibile in regime laterale.
- Impatto P&L: il filtro `rsi_extreme` e le regole nel prompt scattano leggermente "fuori fase" rispetto a quello che leggi su un grafico standard.
- Raccomandazione: passare a Wilder smoothing (ricorsivo) o usare `pandas_ta` / `ta-lib`. Modifica una funzione di 10 righe.

#### P1.3 Discovery dinamica importa asset di cui non si conosce la liquidità
- File: `src/discovery.py:111-190`
- `discover_top_movers` legge il nodo `crypto_currencies` di Capital (tutti, ~265 mercati) e i nodi `popular_shares` / `us.most_volatile`. Il filtro è solo `marketStatus == TRADEABLE` e `|pct| >= 2`. Niente filtro di spread, niente filtro di volume minimo, niente capping di market cap per crypto. È esattamente il vettore con cui le alt-coin micro-cap sono entrate nel weekend pre-allowlist. Ora l'allowlist sabato/domenica difende, ma in settimana resta aperta la porta a small cap stock e crypto micro-cap durante il pre-filter (può uscire un mover +25% con spread 3%, RSI 75, e arrivare al LLM).
- Impatto P&L: spread alto + slippage in apertura + setup blow-off = perdita strutturale anche con stop "tecnicamente" corretto.
- Raccomandazione: hard filter in `_collect_features` o `_prefilter_candidates` su:
  - `spread_pct <= 0.30%` per major asset, `<= 0.80%` per crypto major, mai sopra `1.5%`.
  - per crypto: allowlist anche infrasettimanale, oppure whitelist di "top 30 per market cap".
  - per share: solo se prezzo > 5 USD e il nodo è `popular_shares` (escludere `us.most_volatile`, è un magnete di pump).

#### P1.4 Nessun controllo di correlazione fra trade aperti oltre il binario risk_on/risk_off
- File: `src/macro_guard.py:37-77`
- Il guardrail correlation usa solo 3 gruppi: `risk_on`, `risk_off`, `neutro`. Ma BTC e ETH sono fortemente correlati (corr giornaliera tipica 0.85+); WTI e Brent sono praticamente lo stesso strumento (corr 0.97+); US500 e Nasdaq 100 sono correlati 0.90+. Il guardrail penalizza -1.5 score se aprono nello stesso macro_group, ma se hai già BTC long e arriva un setup ETH long con score 8.5, la penalty -1.5 lo porta a 7.0 - sufficiente per passare la soglia 7 e raddoppiare il rischio sulla stessa scommessa.
- Impatto P&L: drawdown amplificato in mosse correlate.
- Raccomandazione: per coppie altamente correlate (BTC/ETH, WTI/Brent, US500/Nasdaq, Gold/Silver) penalty maggiore (-3 o drop) se direzione concorde; oppure trattarle come "stesso slot" del `MAX_OPEN_POSITIONS`.

#### P1.5 Bias del prompt verso produzione di setup (overconfidence)
- File: `src/llm_analyzer.py:62-173`
- Il prompt chiede "Produci un ranking dei top 3 setup" e poi "Sii selettivo. Se nessun asset ha setup decente, restituisci tutti score sotto 6". Ma la formulazione "top 3" è un anchor: il modello tende a riempire 3 slot perché il pattern di output `proposals: [...]` con almeno 1-2 elementi è premiato dalla coerenza testuale. La regola weekend "se nessun crypto major weekend soddisfa TUTTI i criteri sopra, restituisci proposals con direction='skip' o lista vuota" è un buon antidoto, ma è applicata solo nel weekend.
- Impatto P&L: in giornate piatte/laterali il sistema produce comunque score 6.5-7 che passano la soglia 7 perché il LLM non vuole "sembrare inutile".
- Raccomandazione: nella fase di prompt rework, esplicitare sempre "in giornate senza setup chiari restituisci `proposals: []`. Non riempire output. Score 7 deve avere un trigger tecnico identificabile". Inoltre alzare `MIN_SCORE_THRESHOLD` da 7.0 a 7.5 fino a quando i bucket non dimostrino edge a 7.0.

#### P1.6 Trailing stop senza time stop
- File: `src/position_monitor.py:73-237`
- Trailing R-multiple ben fatto, ma una posizione che resta tra +0.3R e +0.7R per giorni non viene mai chiusa né protetta a BE. Lo swing trade si trasforma in position trade involontario, esposto a news e weekend.
- Impatto P&L: occupy `MAX_OPEN_POSITIONS=1` con trade dormiente, perdita di opportunità (carico-opportunità) e crescente esposizione a eventi.
- Raccomandazione: time stop adattivo. Se a 48-72h dall'apertura il profit_R non ha mai toccato 1R, chiudere automaticamente o forzare BE. È una regola robusta nel literature di swing trading (es. Kaufman, Aronson).

#### P1.7 Trailing aggressivo (step_r=0.5) può uccidere trade vincenti per rumore
- File: `src/config.py:39,116` (default 0.5), `src/position_monitor.py:73-237`
- Con `step_r=0.5`, ogni 0.5R sopra il BE muove lo SL di 0.5R verso l'alto. Su asset volatili (crypto) un retracement normale del 30-40% del movimento ti spezzona. Su un setup R:R 1:3, ti accontenti spesso di +1R quando il target era +3R.
- Impatto P&L: expectancy ridotta per trade vincenti tagliati troppo presto.
- Raccomandazione: trailing scelto per asset class. Crypto/commodity volatili: `step_r=1.0`. FX/indici stabili: `step_r=0.5`. Oppure trailing ATR-based (sposti SL a `current_price - 2*ATR_4h`) invece che R-based.

#### P1.8 Pre-filter `pct_from_high_20` near_high vs correction non distingue lato del trade
- File: `src/scanner.py:498-509`
- Il filtro accetta sia `|pct| <= 1` (vicino ai massimi, breakout potenziale) che `pct <= -8` (correzione, bounce potenziale). Ma poi il LLM decide la direzione: lascia entrare candidati che potrebbero essere short su un asset vicino ai massimi (controproducente sui breakout) o long su un asset in correzione profonda (beccare il falling knife).
- Impatto P&L: setup invertiti rispetto alla logica del filtro.
- Raccomandazione: passare al LLM una hint diretta `pre_filter_reason` per ogni candidato (es. "near_high - breakout setup probabile", "correction - bounce setup probabile") così che il modello allinei la direzione, oppure pre-direzionare la proposta a livello di filtro.

### P2 - Medio

#### P2.1 News usate come testo grezzo, mai come sentiment quantificato
- File: `src/scanner.py:907-928` (arricchimento news), `src/llm_analyzer.py:65-66`
- Le news arrivano al LLM come stringhe (`headline`, `source`, `datetime`). Il modello deve fare sentiment analysis al volo. Funziona ma è rumoroso e i dati non sono auditabili (non sai quale headline ha pesato sul score).
- Raccomandazione: pre-pipeline che estrae sentiment numerico (-1/0/+1) per ogni news prima del payload LLM. Conservare il sentiment nel signal in DB per analisi correlazione score/pnl.

#### P2.2 Macro guard non considera la stagionalità intraday
- File: `src/scanner.py:810-820`, `src/quiet_hours.py`
- Lo scanner gira ogni ora 7-22, ma alcune fasce orarie hanno edge negativo strutturale: opening US (15:30-16:30 IT) per indici è caotico, chiusura US (21:30-22:00) idem. L'overnight session forex ha spread allargati.
- Raccomandazione: sotto-finestre attive per asset class. Non aprire indici US fra 15:30 e 16:30 e fra 21:30 e 22:00. Non aprire forex tra 22:00 e 00:00 (spread allargato in rollover).

#### P2.3 Reconcile inline può alterare il conteggio MAX_OPEN_POSITIONS in race condition
- File: `src/position_monitor.py:485-501`, `src/scanner.py:1057-1061`
- Il monitor reconcile chiude trade nel DB, lo scanner conta posizioni aperte da Capital. Se Capital non ha ancora propagato la chiusura ma il DB sì, lo scanner potrebbe leggere `len(open_positions) = 1` dal Capital e considerare lo slot pieno, oppure leggere 0 ma il trade DB è ancora "open" per metà secondo.
- Impatto P&L: minore (race window stretta), ma può causare uno skip o un'apertura doppia rara.
- Raccomandazione: nello scanner, usare il count di Capital come fonte di verità (è già così). Documentare nel codice che il DB serve solo per audit e non per `max_open_positions` count.

#### P2.4 Nessun logging delle feature al momento del signal
- File: `src/db.py` e `src/scanner.py:1064-1078`
- `insert_signal` salva solo `asset`, `direction`, `score`, `thesis`, `entry_price`, `stop_loss`, `take_profit`. Non vengono salvate le feature tecniche (RSI, ATR, slope, daily_pct_change, spread, news headlines) al momento della decisione. Senza queste è impossibile fare backtest retrospettivo o analisi causale (es. "i loss avevano tutti spread > 0.5%?").
- Raccomandazione: aggiungere colonna `features_at_decision JSONB` a `signals` (Supabase migration) e popolarla con il dict completo di feature al momento del signal. Costo storage trascurabile, valore diagnostico enorme.

#### P2.5 Macro guard correzione score fatta DOPO il sort iniziale del LLM
- File: `src/scanner.py:973-999`
- L'LLM produce `proposals` ordinate per score, poi `apply_macro_guardrails` riduce alcuni score, poi `eligible.sort(key=lambda p: p.score, reverse=True)` riordina. OK. Però il prompt LLM è già stato istruito a applicare le stesse regole macro: il rischio è la doppia-penalizzazione (LLM lo abbassa, macro_guard lo riabbassa). Confronta gli score "raw" LLM con quelli post-guardrail per capire se le regole stanno doppiando.
- Raccomandazione: scegliere uno solo dei due livelli (preferibilmente Python deterministico) e nel prompt rimuovere le regole macro che vengono applicate poi in `macro_guard.py`. Più chiarezza, meno doppi conteggi.

#### P2.6 `min_score_threshold = 7` e prompt che parla di "8+ eccellenti, 6-7 buoni"
- File: `src/config.py:109`, `src/llm_analyzer.py:111`
- Il LLM tarata su 6-7 = "buoni" produce molti 7.0-7.5 che passano. Senza dati di hit-rate per bucket calibrati, è una soglia di comodo, non scelta empiricamente.
- Raccomandazione: dopo accumulo di 30+ trade, ricalibrare la soglia leggendo `compute_hit_rate_by_score`. Per ora impostare prudenzialmente 7.5.

### P3 - Nice to have

#### P3.1 Risk per trade non standardizzato in % del capitale
- File: `src/risk.py`, `src/config.py`
- Il sizing è guidato da `exposure_budget_eur` (margine bloccato), non da un risk-per-trade in % del balance (es. "rischia 1% del capitale per trade"). Funziona ma rende difficile parlare in termini standard di money management.
- Raccomandazione: parametro alternativo `RISK_PER_TRADE_PCT`. Calcolo size: `risk_eur = balance * pct / 100`, `size = risk_eur / (entry * stop_pct / 100)`. Più chiaro e scalabile col balance.

#### P3.2 No metrica di Sharpe / Sortino / profit factor nel weekly
- File: `src/performance.py`
- Solo win rate, total pnl, max drawdown. Mancano expectancy in R, profit factor (gross profit / gross loss), Sharpe ratio.
- Raccomandazione: aggiungere `expectancy_r`, `profit_factor`, `sharpe` (calcolato sui ritorni giornalieri).

#### P3.3 Quiet hours hardcoded nel codice
- File: `src/quiet_hours.py:18-20`
- 7:00-22:30 weekday, 9:00-22:30 weekend. Non parametrizzati.
- Raccomandazione: env vars `QUIET_START_*`, `QUIET_END_*`. Bassa priorità.

#### P3.4 News dedup non semantico
- File: `src/news.py` (presunto, non letto in dettaglio)
- Probabile assenza di dedup semantico (stessa news riportata da 5 RSS = 5 entry distinte). Il LLM vede ridondanza.
- Raccomandazione: hashing del titolo o similarity check < 0.85 prima di passare al payload.

## 3. Quick wins per migliorare hit rate / ridurre loss rate

In ordine di sforzo crescente, tutti implementabili in <1 giornata:

1. Allineare prompt LLM al payload effettivo (P0.1, opzione B): rimuovere riferimenti a EMA, BB upper/lower, supporti/resistenze. Stop confabulazione.
2. Hard filter spread + ATR ratio nel pre-filter: scartare a monte asset con `spread_pct > 0.5%` (non-crypto) o `> 1.5%` (crypto), e asset con `atr_pct_of_price < 0.3%` (mercato dormiente, target irraggiungibili).
3. Stop adattivo ATR-based: `effective_stop_pct = max(llm_stop_pct, 1.5 * atr_pct_of_price)`.
4. Time stop: chiusura automatica a 72h se profit_R non ha mai toccato 1R.
5. Daily/weekly loss cap: hard stop dello scanner se `pnl_today < -X EUR` o `pnl_week < -Y EUR`.
6. Alzare `MIN_SCORE_THRESHOLD` a 7.5 finché i bucket non mostrino edge a 7.
7. Salvare `features_at_decision` in `signals` (per backtest retrospettivo).

## 4. Cambi strutturali della strategia

### 4.1 Layer rule-based prima dell'LLM
Oggi il filtro pre-LLM è puramente sui mover (daily_pct, RSI estremo, BB compression). Manca un check di "regime di mercato" sull'indice di riferimento dell'asset class:
- prima di proporre setup su S&P/Nasdaq/DAX, verificare il trend del VIX (se VIX > 25, ridurre score di indici long, alzare per gold/safe haven).
- prima di proporre crypto, verificare BTC dominance e BTC trend (alt-coin vanno bene solo in regime risk-on con BTC stabile).
- prima di proporre forex, verificare DXY trend.

Senza questo "regime filter", l'LLM è cieco al contesto macro più importante (e il prompt non ha visibilità su VIX, DXY, BTC.D).

### 4.2 Backtest harness
Oggi non c'è. Senza, ogni decisione di tuning è speculativa. Suggerimento: snapshot delle feature giornaliere via cron (anche solo per UNIVERSE statico), salvataggio in tabella `feature_snapshots`, replay dei signal su quelle feature con il prompt del giorno, e produzione di un equity curve simulata. Anche un backtest grezzo di 30-60 giorni di dati salvati permette di sondare se le modifiche al prompt hanno impatto.

### 4.3 Ridurre l'universo a quello che hai capito
Più asset = più rumore. Per fase Coach, restringere drasticamente: 3-4 asset top (BTC, ETH, US500, Gold) e basta. Solo dopo 100 trade si può ragionare su universo allargato. La discovery dinamica è una feature potente ma prematura: aumenta il sample diluendolo su asset eterogenei.

### 4.4 Distinzione fra "scoring quantitativo" e "razionalizzazione narrativa"
L'LLM è ottimo nel SECONDO ma scadente nel PRIMO. Architettura migliore:
- Layer 1 (Python deterministico): genera il punteggio quantitativo dall'analisi tecnica - ATR, RSI Wilder, breakout bool, trend coerente bool, R:R minimo. Outcome: score 0-10 deterministico.
- Layer 2 (LLM): riceve il punteggio quantitativo e le feature, e produce SOLO la thesis testuale, key_factors, risks. Lo score NON è negoziabile dal LLM, lo eredita dal layer 1.
- Effetto: l'utente legge un razionale leggibile ma il filtro decisionale è oggettivo e auditabile.

## 5. Domande aperte da porre al trader/operatore

1. Qual è il tuo capitale di rischio totale per TradeAlert? Senza saperlo, non posso calibrare i cap di drawdown in valore assoluto.
2. Quale è la tua expectancy minima accettabile in R per considerare il sistema "funzionante"? (suggerimento: > +0.3R con winrate > 40%, ma è funzione della tua tolleranza al drawdown).
3. Quanti trade sei disposto a vedere prima di concludere "il sistema non funziona"? Sotto i 50 trade è troppo presto per qualsiasi conclusione.
4. Hai un benchmark? (es. "almeno meglio che hold BTC" o "almeno meglio che cash"). Senza benchmark non distingui edge da bull market.
5. Vuoi che il sistema funzioni in TUTTE le condizioni di mercato, o solo in trend? Strategie trend-following falliscono strutturalmente in mean reversion regime: meglio dichiararlo.
6. Su 100 EUR di rischio, quanto è il tuo "uncle point" mensile (drawdown max che ti farebbe spegnere il sistema)? Va parametrizzato in `MAX_MONTHLY_DRAWDOWN_PCT`.
7. Hai necessità di tassazione/dichiarazione delle plusvalenze CFD? Capital fa report? Se sì, va loggato anche il rendimento per anno fiscale.
8. La scelta di Capital.com è strategica o casuale? Spread su micro-cap crypto sono fra i peggiori del mercato. Per crypto major potrebbe valere la pena scindere su exchange spot dedicati.
9. Vuoi considerare l'introduzione di un secondo modello di scoring (es. confronto Haiku vs Sonnet su un campione, per stimare lift/cost)? Adesso il monitor usa Haiku, l'analyzer usa lo stesso (default Haiku). Sonnet sul ranking potrebbe essere giustificabile se elimina i 7.0 confabulati.
10. Sei disposto a ridurre l'universo alle 4-5 cose che capisci meglio per i prossimi 60 giorni? È la singola modifica con più impatto su qualità dei dati.
