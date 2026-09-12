# TradeAlert — Overview e storia di sviluppo

Documento di sintesi sullo stato dell'applicativo al 2026-04-27 e sul percorso seguito nei primi 11 giorni di sviluppo (17–27 aprile 2026, 64 commit complessivi).

## 1. Cosa fa TradeAlert

TradeAlert è un sistema di intelligence di mercato e gestione di swing trade su Capital.com. Il flusso di lavoro è:

1. A intervalli regolari scansiona un universo ristretto di asset (oggi 5 strumenti core: Gold, Brent Oil, US500, Nasdaq 100, Bitcoin) e calcola feature tecniche standard su candele 4H (RSI, ATR, slope di regressione, ampiezza Bollinger, distanza dai massimi 20 candele, momentum giornaliero).
2. Passa il payload a Claude (Haiku, modello "fast") che produce un ranking dei setup con score 0–10, direzione long/short, stop e target in percentuale, thesis e fattori chiave.
3. Applica guardrail server-side post-LLM (allargamento stop in prossimità di eventi macro, blow-off filter, dedup giornaliero, cap settimanale di drawdown, vincolo di accessibilità del setup).
4. Se un setup supera la soglia di score e tutti i filtri, il sistema invia su Telegram un messaggio strutturato con bottoni: budget preset (10–30 EUR di margine), Esegui, Salta. In modalità `confirm` l'apertura della posizione su Capital avviene solo dopo il click.
5. Una volta aperto il trade, un position monitor con cadenza 30 min decide HOLD o CLOSE consultando di nuovo l'LLM, mentre un trailing stop più leggero gira ogni 5 min H24 spostando lo SL verso half-risk, breakeven e oltre senza coinvolgere l'LLM.
6. Tutto lo storico viene scritto su Supabase: signals, trades, scanner_runs, monitoring_events, account_snapshots. Le decisioni LLM portano il payload tecnico originale in `signals.features_at_decision` per analisi retrospettiva.

L'execution mode attuale è `confirm`: l'utente conferma manualmente da Telegram. Le modalità `coach` (solo segnali, niente esecuzione) e `auto` (full-auto con limiti hard) sono previste ma non in uso oggi.

## 2. Stack e infrastruttura

| Componente | Scelta | Note |
|---|---|---|
| Cron e listener | VM Oracle Free Tier (Ubuntu 22.04) | migrato da GitHub Actions in fase MVP per latenza e affidabilità |
| Storico | Supabase (Postgres) con RLS abilitato | migrazioni in `supabase/migrations/` versionate con timestamp UTC |
| LLM | Anthropic Claude Haiku (fast) per scoring, monitor, news | Sonnet riservato a task specifici, ridotto per contenere i costi |
| Broker | Capital.com REST API (CST + X-SECURITY-TOKEN) | account live CFD EUR |
| Notifiche | Telegram Bot API | listener daemon con offset persistente, niente polling concorrente |
| News | RSS grezzi via `src/news.py`, classificazione LLM solo nel briefing |  |
| Calendario macro | Finnhub economic calendar | usato per allargare stop pre-evento |

## 3. Layout del codice

```
src/                          # moduli applicativi
  scanner.py                  # cuore, ~1340 righe: orchestra scan, LLM, guardrail, signal write
  position_monitor.py         # monitor LLM HOLD/CLOSE + trailing R-multiple
  executor.py                 # apertura posizione su Capital, sizing, min stop distance
  confirm_handler.py          # callback bottoni Telegram (Esegui/Budget/Skip)
  command_listener.py         # daemon long-polling Telegram per comandi /status, /posizioni
  capital_client.py           # wrapper REST Capital.com, cache /accounts/preferences (TTL 1h)
  features.py                 # feature tecniche da candele + min_size/margin_factor
  risk.py                     # calculate_size con doppio vincolo budget + max_loss_per_trade
  llm_analyzer.py             # prompt scoring setup, parsing JSON robusto
  news_analyzer.py            # classificazione news (chiamata solo dal briefing oggi)
  macro_guard.py              # blow-off, dedup, allargamento stop, allowlist weekend
  market_context.py           # snapshot multi-asset per contesto macro nel prompt
  event_extractor.py          # critical events automatici da news (LLM)
  discovery.py                # scoperta dinamica top mover (oggi disabilitata, Fix 1.3)
  watchlist.py                # mappa epic Capital con fallback search
  status.py                   # comando /status Telegram
  briefing.py                 # mattina/pomeriggio/sera (oggi disabilitato per costi)
  reconcile.py                # housekeeping DB vs Capital
  performance.py              # weekly report metriche
  quiet_hours.py              # finestre attive
  config.py, db.py, telegram_client.py, positions.py, health.py, universe.py

jobs/                         # entrypoint chiamati dal crontab
  morning_scan.py             # scanner ogni ora 7-22
  monitor.py                  # position monitor ogni 30 min
  trailing_stop.py            # trailing leggero ogni 5 min H24
  reconcile.py                # ogni ora
  macro_scan.py               # 06:50 ogni giorno
  health_check.py             # 09:10 ogni giorno
  weekly_report.py            # domenica 20:00
  briefing.py                 # disabilitato
  listen.py                   # daemon listener Telegram (systemd, non cron)

supabase/migrations/          # 6 migration: init, scanner_runs, signals.epic, RLS,
                              # scanner_runs.outcome=risk_cap, features_at_decision JSONB

deploy/                       # crontab.txt, setup_vm.sh, tradealert-listener.service
config/critical_events.json   # eventi macro hardcoded di backup
```

## 4. Universo e cadenze attuali

Universo (`src/universe.py`, scelta del Fix 1.3):

| Asset | Epic | Asset class | Note |
|---|---|---|---|
| Gold | GOLD | metal | safe haven |
| Brent Oil | OIL_BRENT | energy | commodity ciclica |
| US500 | US500 | index | risk-on US |
| Nasdaq 100 | US100 | index | risk-on US tech |
| Bitcoin | BTCUSD | crypto | unico H24, weekend allowlist |

Crontab (`deploy/crontab.txt`):

```
*/5 * * * *      jobs.trailing_stop     # H24, no LLM
5 7-22 * * *     jobs.morning_scan      # ogni ora finestra attiva
*/30 7-22 * * *  jobs.monitor           # HOLD/CLOSE LLM
50 6 * * *       jobs.macro_scan        # estrae critical events
15 * * * *       jobs.reconcile         # DB vs Capital
10 9 * * *       jobs.health_check
0 20 * * 0       jobs.weekly_report
```

Listener Telegram come servizio systemd `tradealert-listener.service` (non in cron) per ricevere i click dei bottoni in tempo reale.

## 5. Modello di rischio (parametri Sprint 1, immutabili)

- **Capitale rischio totale**: 100 EUR (kill switch finale, fuori scope Sprint 1).
- **Cap settimanale drawdown**: 20 EUR rolling 7gg sui trade chiusi. Costante `WEEKLY_DRAWDOWN_CAP_EUR=20.0` in `src/config.py`. Se superato, lo scanner si auto-stoppa, scrive `scanner_runs.outcome='risk_cap'` e notifica Telegram una sola volta per giorno UTC.
- **Cap perdita per trade**: 5 EUR (env `MAX_LOSS_PER_TRADE_EUR`, aggiunto 2026-04-27). Il sizing usa il vincolo più restrittivo tra budget di margine e max loss; se la `min_size` del broker non rispetta il cap, il setup viene scartato come "non eseguibile entro risk cap".
- **Soglia score**: 7.0 di default (env `MIN_SCORE_THRESHOLD`).
- **Universo ristretto**: 5 asset core, sample target 30–50 trade chiusi prima di rivisitare le soglie.
- **Discovery dinamica**: disattivata fino al raggiungimento del sample target.
- **Weekend allowlist**: solo Bitcoin per evitare i micro-cap weekend pattern (vedi memory `weekend_signal_pattern`).

Coerenza dei numeri: con max_loss €5 e cap settimanale €20, servono 4 SL pieni consecutivi per fermare lo scanner; 20 SL pieni per esaurire il capitale di rischio totale.

## 6. Storia di sviluppo (11 giorni, 64 commit)

### Settimana 1 (17–18 aprile): MVP e infrastruttura

Il primo commit è il `Initial commit` del 17 aprile. Il giorno dopo arriva l'MVP: `577245e MVP: scanner multi-asset, sizing per budget, esecuzione/conferma Capital, briefing news`. Nelle 24 ore successive viene aggiunta crypto (BTC/ETH), si scelgono le finestre attive e le cadenze del cron, si aggiunge il position monitor con quiet hours, si separa il listener Telegram come daemon con offset persistente, si fa la migrazione completa da GitHub Actions a VM Oracle Free Tier per ridurre la latenza dei comandi e i tempi di start dei job. In meno di due giorni esiste già un sistema end-to-end funzionante.

### Settimana 1 (19–21 aprile): qualità del segnale e robustezza

Il focus si sposta sulla qualità dello scoring e sull'affidabilità delle chiamate esterne. Vengono aggiunti: rotation con soglia adattiva al P&L della posizione peggiore, retry login Capital su 429 e errori di rete, throttle 150ms tra chiamate, comando `/status` Telegram con tracking esito scanner, scrittura dei `proposals` LLM in `scanner_runs.notes` per audit, salvataggio dell'epic direttamente su `signals` per evitare lookup ripetuti, daily_pct_change come feature dedicata per riconoscere i mover intraday. Trailing stop a 0.5R con logging esteso. Confirm handler che ricontrolla il rapporto rischio/rendimento prima dell'esecuzione (perché il prezzo si è mosso tra signal e click). RLS abilitato su Supabase + supporto service_role key per i job server-side.

### Settimana 2 (22–24 aprile): macro awareness e contenimento costi

Si aggiunge il calendario macro Finnhub (`a94eb74 Feature 2 completa`), un guardrail post-LLM che allarga lo stop se è imminente un evento ad alto impatto, e un `event_extractor` che usa l'LLM per estrarre eventi critici dalle news quando il calendario è scarico. La trasparenza Telegram mostra all'utente quale guardrail è scattato. Costi LLM: pre-filtro asset prima dello scoring, monitor passato a Haiku, news con truncate, prompt compatti, briefing arricchito da Haiku prima della sintesi finale. Il 24 aprile vengono disabilitati i briefing 3x/giorno (`90630cd`) per risparmio API. Cadenza scanner ridotta da 30' a 60', cap a 8 asset prima del LLM ranking.

### Settimana 2 (25–26 aprile): qualità weekend e Sprint 1

Il 25 aprile arriva `40363f2 Weekend signal quality: allowlist crypto major + blow-off filter + prompt riallineato`. Viene osservato che nei weekend i segnali tendono a saltare su alt-coin micro-cap con spread ampio, statistica 5 chiusi 0 win al 25 aprile. Si introduce un blow-off filter, l'allowlist crypto major, e si ricalibra il prompt. Lo stesso giorno parte lo Sprint 1 con la "configurazione cristallizzata" in 4 fix:

- **Fix 1.1** (`9b5a761`): allinea il prompt LLM al payload reale. Pre-fix il prompt menzionava EMA20/50/200, banda upper/lower Bollinger, livelli S/R numerici, volume — tutte feature non presenti nel payload. Era un'allucinazione invitata.
- **Fix 1.2** (`a43c05c`): cap settimanale drawdown -20 EUR con auto-stop scanner.
- **Fix 1.3** (`ce14b7a`): universo ridotto a 5 asset core, discovery disabilitata.
- **Fix 1.4** (`7ff3fdf`): colonna JSONB `signals.features_at_decision` per persistere il contesto tecnico delle decisioni e abilitare backtest retrospettivi.

`6acccc5` aggiorna il README con la configurazione cristallizzata.

### 27 aprile: il bug nascosto del margin factor e il cap perdita per trade

Mattina del 27 aprile: il top setup Brent score 7.5 dello scan delle 08:05 non viene promosso a signal. Investigando il log emerge la riga `skip budget: Brent Oil: servono ~101 EUR di margine minimo (tuo budget max 30 EUR)`. La causa apparente sembra "Brent troppo costoso per il budget", ma a un'analisi più profonda il margine richiesto andava interpretato con la leva reale, non con il `marginFactor` statico restituito dall'API `/markets/{epic}` (sempre 100% per tutti gli strumenti su questo broker, è un dato di prodotto, non di account).

L'endpoint `/accounts/preferences` rivela la verità: l'account ha leva 20x su commodities/indices, 30x su FX, 2x su crypto. Con margin factor effettivo `1/leverage`, Brent richiede in realtà ~5 USD di margine, non 101. Lo stesso vale per Gold (~2 USD) e US500 (~3.6 USD). **Tre asset su cinque dell'universo core erano de facto morti per un bug di parsing**, non per ragioni di prezzo.

`7b67514 Fix margin factor parsing + max loss per trade cap` risolve in due colpi:

1. `CapitalClient.get_leverages_map()` con cache TTL 1h, e `effective_margin_factor()` in `risk.py` che usa la leva reale dell'account con fallback al vecchio comportamento se l'endpoint fallisce. Mappa asset_class TradeAlert verso instrument.type Capital (metal/energy → COMMODITIES, index → INDICES, crypto → CRYPTOCURRENCIES, fx → CURRENCIES).
2. `max_loss_per_trade_eur` (default 5 EUR, env override) in `calculate_size`. Cappa la size al minimo tra budget di margine e perdita massima accettabile, con floor sul rounding per non oltrepassare mai il vincolo. Se la `min_size` del broker forza una perdita potenziale > cap, il setup viene scartato come "non eseguibile entro risk cap".

Al primo run scanner post-deploy arriva `signal id=58: Brent Oil long`, finalmente promosso. Sizing capped dal max_loss: size 1.9 (invece di 2.9 dal solo budget), rischio 4.82 USD (≈ 4.11 EUR) sotto cap.

### 27 aprile, sera: il TP che spariva al primo trailing

Pochi minuti dopo l'apertura del trade Brent, alle 20:05:12, il trailing scatta a profit 0.52R applicando la regola half-risk (SL spostato a entry - 0.5R, da 98.841 a 100.152). Ispezionando la posizione live su Capital, `profitLevel` risulta `null`. Sull'activity history compare un evento `EDIT_STOP_AND_LIMIT` con nei `details` solo `stopLevel`, niente `profitLevel`.

La causa è in `position_monitor._apply_trailing_stop`, che chiamava `capital.update_position(deal_id, stop_level=new_sl)` senza ripassare il `profit_level`. `CapitalClient.update_position` costruisce il body PUT con i soli campi non-None, quindi il body conteneva solo `stopLevel`. L'API Capital `PUT /positions/{dealId}` interpreta i campi mancanti come `null` e azzera il `profitLevel`. Risultato: il TP veniva cancellato al PRIMO trigger trailing (anche a 0.5R half-risk), non al breakeven a 1R come si poteva ipotizzare osservando il pattern.

Il fix (`2aa219a` + `bda0361`) preleva `pos.profitLevel` dallo state live del broker e lo ripassa sempre nel PUT. La sorgente è broker e non DB (`trade.current_tp`) per non resuscitare retroattivamente i TP già persi sui trade legacy. Il log differenzia `TP X preserved` da `broker TP=None (no preserve)` per riconoscere a colpo d'occhio se un trade è pre-fix o post-fix.

Implicazione storica: tutti i trade chiusi prima del 27 aprile potevano arrivare al breakeven o oltre senza mai chiudersi a TP — il TP era già stato cancellato a 0.5R. Qualunque analisi di hit rate basata sul "TP raggiunto vs SL hit" pre-fix va riletta tenendo conto che il TP non era mai realmente in vita oltre il primo trigger trailing.

## 7. Punti aperti e direzioni naturali

- **Sample size**: 30–50 trade chiusi sull'universo da 5 asset prima di rivisitare la soglia di score, le cadenze e l'attivazione di discovery. Oggi siamo nei primi 20.
- **Monitoring del fix margin factor**: verificare nei prossimi 7–14 giorni che Gold, US500 e Nasdaq 100 (oltre a Brent) vengano effettivamente proposti come signal — pre-fix erano de facto invisibili.
- **Verifica fix trailing TP**: al primo trade post-fix che entra in trailing, controllare l'activity Capital `EDIT_STOP_AND_LIMIT` e confermare la presenza simultanea di `stopLevel` e `profitLevel` nei details. Comando documentato in chat.
- **Currency-blind**: tutti i calcoli di rischio trattano i numeri come se fossero in EUR, anche se Brent, Gold, US500 sono quotati in USD. Lo skew è ~15% (rate EUR/USD ≈ 1.17). Per il volume di rischio attuale è acceptable, ma andrà reso esplicito quando si scalerà.
- **Briefing**: spenti per costi. Riabilitabili con env `BRIEFING_ENABLED=true` + scommentare le 3 righe in `crontab.txt`.
- **Modalità auto**: prevista in roadmap, oggi inutile finché il sample size non valida l'edge.
