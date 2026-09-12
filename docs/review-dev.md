# TradeAlert, review tecnica

Autore: review senior Python / SRE su richiesta del team-lead.
Data: 2026-04-25.
Scope: codice sotto `src/`, `jobs/`, `supabase/migrations/`, `deploy/`, `.github/workflows/`.

## 1. Sintesi

TradeAlert è un MVP coerente con il suo obiettivo: orchestrare scanner, monitor, executor e listener Telegram con cron classico su VM Oracle, con Supabase come storage e Claude Haiku come ranker dei setup. La struttura per moduli è leggibile, le entry point in `jobs/` sono sottili e delegano correttamente alla logica in `src/`, il flusso di un signal (discovery, feature, pre-filter, LLM, guardrails, dedup, rotation, esecuzione) è ricostruibile leggendo `src/scanner.py:810` in sequenza. Il filtro blow-off weekend, l'allowlist crypto major e il pre-filter deterministico sono interventi sensati per contenere costi LLM e qualità segnale.

I rischi principali sono concentrati su tre fronti. Primo, sicurezza/operatività dello stack DB: il flusso GitHub Actions passa solo `SUPABASE_ANON_KEY` ai job (nessun secret per la `service_role`), quindi con RLS attivo i fallback su Actions falliscono silenziosamente in scrittura; questo combinato con la totale assenza di test automatici lascia il sistema senza rete di sicurezza. Secondo, race condition: ogni 5 min `trailing_stop` apre una sessione Capital, ogni 30 min `monitor` ne apre un'altra che fa anch'essa trailing più reconcile inline, e su una stessa posizione possono insistere update concorrenti senza lock applicativo. Terzo, costi LLM ancora ottimizzabili: il system prompt di `llm_analyzer.py` (~3 KB statici) viene rimandato a ogni chiamata senza prompt caching Anthropic. La codebase è scritta in modo lineare, dipende molto da `Anthropic` istanziato ad-hoc e non ha tracciabilità (logging non strutturato, niente correlation id).

## 2. Findings prioritizzati

### P0, critico

#### P0-1 — `SUPABASE_SERVICE_ROLE_KEY` non passata ai workflow GitHub Actions
- File: `.github/workflows/morning-scan.yml:35`, `position-monitor.yml:37`, `listen-commands.yml:39`, `briefing.yml:56`; uso in `src/db.py:24`.
- Problema: i workflow forniscono solo `SUPABASE_URL` e `SUPABASE_ANON_KEY`. La migration `20260421230000_enable_rls.sql` abilita RLS su tutte le tabelle senza alcuna policy per `anon`. `Database.__init__` cade sulla anon key e logga warning, ma le insert/update finiscono in errore (in client `supabase-py` la response data è vuota, non viene sollevato sempre). I job di fallback Actions, in caso di disastro VM, non riuscirebbero a persistere nulla.
- Impatto: persistenza muta, `recent_signal_assets` ritorna sempre vuoto -> dedup giornaliero saltato, `scanner_runs` non popolato -> `/status` regredisce. Il rischio si concretizza nel momento in cui si invoca un workflow manualmente o in caso di failover.
- Raccomandazione: aggiungere `SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}` ai quattro workflow e marcare la anon key come legacy. In `config.py:92` rendere `SUPABASE_ANON_KEY` opzionale (`os.environ.get(... , "")`) visto che lato server non serve.

#### P0-2 — Race condition trailing stop, monitor e reconcile concorrenti
- File: `src/position_monitor.py:434` (`run_trailing_stops`, ogni 5 min), `src/position_monitor.py:468` (`monitor_positions`, ogni 30 min, riapplica trailing + reconcile inline), `deploy/crontab.txt:25`.
- Problema: trailing scatta ogni 5 min H24 (giustamente, perché crypto è H24); il monitor 30-min apre la propria sessione Capital, riapplica trailing prima del LLM, poi nel listener daemon su click `mclose` parte un terzo `CapitalClient.login()`. Capital ha rate limit ~1 req/sec sulla session creation; più sessioni concorrenti possono entrare in race su `update_position` (es. monitor sposta SL a +1R, mentre il trailing già lo aveva mosso a +0.5R). Inoltre il daemon listener e questi cron condividono il bot Telegram con `getUpdates`: il listener è in long-poll continuo e il monitor non chiama `getUpdates`, ma `manage_positions` (`src/positions.py:88`) sì, e ruba update al daemon (commenta correttamente questo pericolo nel workflow `listen-commands.yml:9`, ma `manage_positions` resta lo stesso).
- Impatto: SL inconsistenti, doppi `update_position`, possibili `409`/`429` da Capital, trailing che retrocede transientemente nel log, comandi `/posizioni` invocati dalla VM che drena update destinati al daemon.
- Raccomandazione:
  1. Eliminare il trailing inline dentro `monitor_positions` (lasciarlo solo nel cron 5-min) — il monitor 30-min può limitarsi al reconcile e alla valutazione LLM.
  2. Sostituire `manage_positions` (polling sincrono) con un comando gestito interamente dal listener daemon (è già il flusso naturale dei callback, basta inviare il messaggio con bottoni e gestire `close:` nel daemon, come si fa con `mclose:`).
  3. Aggiungere un lock filesystem (`fcntl.flock` su `logs/trailing.lock`) o, meglio, un advisory lock Postgres (`pg_advisory_lock`) parametrico su `deal_id` prima di chiamare `update_position`.

#### P0-3 — `confirm_handler` apre una nuova sessione Capital per ogni callback, login chain ridondante
- File: `src/confirm_handler.py:96, :245, :302, :519`.
- Problema: ogni `mclose`, `rot:exec`, `exec`, click budget chiama `CapitalClient(config); capital.login()`. Più click in sequenza (utente che cambia budget tre volte, poi clicca Esegui) generano 4 sessioni in pochi secondi. La funzione `_recompute_sizing_for_signal` apre la sua sessione anche solo per ricalcolare il preview. In più, dentro `_handle_rotation_callback` il login viene rifatto e poi `_fetch_current_mid` lo userebbe ancora — quindi nello stesso percorso si fanno 1 login, 1 fetch market, 1 close, 1 fetch market, 1 create_position.
- Impatto: rate limit Capital, latenza percepita sul bottone, "ricevuto" stranamente lento, in casi limite `429` con messaggio di errore visibile su Telegram.
- Raccomandazione: incapsulare un `CapitalSession` singleton-per-process (con TTL di 5 min) o riusare `_recompute_sizing_for_signal` come prima fase del flusso `exec` invece che chiamarla solo on-budget-click. Per il daemon è ragionevole tenere una sessione in `DaemonState` e rinnovarla on-demand quando una request fallisce per token scaduto.

### P1, alto

#### P1-1 — Zero test automatici, nessun tooling di lint/type-check
- File: tutta la codebase. `requirements.txt` non include pytest/mypy/ruff; non c'è `pyproject.toml`, `pytest.ini`, `setup.cfg`, `ruff.toml`. L'unico file con prefisso `test_*` (`jobs/test_open_close.py`) è uno smoke test manuale che apre una posizione reale.
- Impatto: ogni modifica al sizing, al pre-filter o ai guardrail va in produzione senza rete di sicurezza. Bug come "stop_pct in punti vs in percent" o "direction `short` con segno sbagliato" sono già stati introdotti su sistemi simili.
- Raccomandazione: aggiungere pytest e coprire come minimo: `risk.calculate_size` (caso minimo che blocca, caso disponibile insufficiente), `macro_guard.apply_macro_guardrails` (drop binary, score -2 opposite, stop ×1.5), `quiet_hours.is_quiet_now` (transizioni weekday/weekend), `reconcile._extract_close_info` (long vs short, mancanza pnl). Tutti puri, niente mock di rete necessari. Aggiungere ruff per gli import/unused e mypy in modalità lasca.

#### P1-2 — Prompt caching Anthropic non utilizzato
- File: `src/llm_analyzer.py:62, :198`.
- Problema: il `SYSTEM_PROMPT` è statico (~3 KB, sopra la soglia minima di 1024 token per Haiku 4.5) e viene inviato 1× per scan. Senza `cache_control` ogni chiamata paga il prompt intero. La SDK Anthropic supporta `messages.create(..., system=[{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}}])`.
- Impatto: con scanner ogni ora 7-22 + monitor ogni 30 min con un altro system prompt, sono ~16 chiamate cached/giorno; abilitare il cache su entrambi i system taglia il 70-90% del costo input prompt (10× più economico oltre la prima chiamata) per quel componente.
- Raccomandazione: passare `system` come lista con `cache_control: {"type": "ephemeral"}`. Verificare in fattura il cache hit rate dopo un giorno (Anthropic espone `usage.cache_creation_input_tokens` e `cache_read_input_tokens` nella response).

#### P1-3 — Validazione output LLM fragile, nessun fallback strutturato
- File: `src/llm_analyzer.py:205-228`, `src/position_monitor.py:339-355`.
- Problema: parsing JSON via `json.loads(raw_text)` senza schema validation. Se Claude restituisce JSON malformato (raro ma succede su prompt complessi), `rank_setups` propaga `JSONDecodeError` fino a `run_morning_scan` che invia un messaggio Telegram di errore. Il monitor tace e ritorna `None` (silenzioso, accettabile, ma perde una valutazione). I campi opzionali (`suggested_stop_pct`, `key_factors`) non sono validati come tipo: un LLM creativo che torna `score: "7.5"` come stringa supera il `float()` ma `score: "high"` esplode.
- Impatto: failure mode non graduata. Vale soprattutto perché il monitor non ha retry: una singola risposta malformata salta una potenziale CLOSE.
- Raccomandazione: usare `tool_use` di Claude con un JSON schema stretto, oppure validare con `pydantic` (già nelle dipendenze indirette via `supabase`). Aggiungere un retry singolo con prompt "il tuo output precedente non era JSON valido, riformatta". Loggare sempre `usage` per costo per call.

#### P1-4 — Telegram listener autorizzazione mediocre
- File: `src/command_listener.py:56, :86`, `src/positions.py:76`.
- Problema: l'autorizzazione si basa solo sul confronto `chat_id == telegram_chat_id`. `manage_positions` (invocato sia da daemon che da cron legacy) chiama `wait_for_callback` su un poll proprio, e il listener daemon passa il chat owner al callback ma non al messaggio plain (`/eventi` da un chat extra cadrebbe nel filtro `expected_chat`, però `/evento <data>` arriva via `try_handle_event_command(config, raw_text)` solo se passa il check chat). Codice corretto ma fragile a future modifiche perché la validazione è duplicata in tre punti. Inoltre `TelegramClient` espone `_base` e `_post` come pseudo-privati ma `command_listener` li usa direttamente (`telegram._base`).
- Impatto: il modello di sicurezza vive su una convenzione, non su un'astrazione. Una svista in PR futura rischia di esporre comandi a chat extra.
- Raccomandazione: incapsulare l'authz in un decoratore (`require_owner`) e ridurre la superficie pubblica del client. Spostare il "ack callback" dentro `TelegramClient.answer_callback` invece di duplicarlo in `command_listener._answer_callback`.

#### P1-5 — `confirm_deal` 404 + matching size con tolleranza 1% può creare falsi positivi
- File: `src/executor.py:225-279`.
- Problema: il fallback `same_epic + same_dir + size in ±1%` può matchare la posizione sbagliata se l'utente ha aperto a mano una posizione simile dal frontend Capital nello stesso secondo. Lo size step di alcuni asset è 0.01 e una position con size 0.10 può combaciare con una nostra 0.099 a tolleranza 1%.
- Impatto: bassa probabilità ma alto blast radius (DB punta al deal sbagliato; il successivo trailing/close opera sul wrong trade).
- Raccomandazione: abbassare la tolleranza a 0.1% e richiedere un timestamp di apertura entro N secondi (Capital espone `createdDateUTC`). In alternativa, accettare il fallimento di confirm_deal: marcare il signal come `pending_confirmation` e affidare al reconcile orario il match definitivo.

#### P1-6 — Crypto assets in `MACRO_GROUPS` non perfettamente allineati a `WEEKEND_CRYPTO_MAJOR_ALLOWLIST` e `ASSET_KEYWORDS`
- File: `src/macro_guard.py:38`, `src/scanner.py:44`, `src/news.py:42`, `src/universe.py:36`.
- Problema: ci sono 4 sorgenti di "verità" sulla lista crypto major (universe, allowlist weekend, macro_groups, news keywords). Sono coerenti oggi, ma duplicare richiede attenzione manuale. La universe ha anche 4 alt-coin (`Ethereum Classic`, `EthereumFi`, `EthereumPoW`, `ARPA`) che hanno entry in `ASSET_KEYWORDS` ma non in `MACRO_GROUPS`, quindi non beneficiano del penalty di correlazione.
- Impatto: bassa, ma la prossima aggiunta di un asset richiede 4 modifiche e una svista è probabile.
- Raccomandazione: definire una sola tabella in `universe.py` con tutte le proprietà (`is_weekend_major`, `macro_group`, `keywords`) e derivare le altre 3 strutture da quella.

### P2, medio

#### P2-1 — `src/scanner.py` è un file da 1199 righe con responsabilità miste
- File: `src/scanner.py`.
- Problema: contiene formatting Telegram, business rules (rotation, prefilter), persistence wrapping, orchestrazione e helper di parsing date. Le funzioni `_format_*` sono usate anche da `confirm_handler` (via import locale per evitare cicli).
- Raccomandazione: estrarre `scanner_messages.py` (tutti i `_format_*`, `_direction_label`, `_esc`) e `scanner_rotation.py` (`_maybe_build_rotation_proposal`, `_position_pnl_pct`). Evita import circolari naturali e abbassa la cognitive load.

#### P2-2 — Logging non strutturato, niente correlation id
- File: tutto.
- Problema: tutti i log sono `log.info("Stringa %s", x)`; non c'è un identificativo di scanner_run o di signal_id propagato. Quando `/status` mostra l'esito ma manca un'azione, debuggare richiede `grep` sui log multipli e correlare a manina.
- Raccomandazione: adottare logging JSON (`python-json-logger` o stdlib + filter) con `extra={"run_id": ..., "signal_id": ...}`. Se i log restano file plain, almeno mettere `signal_id=N` come prefisso comune nei messaggi della pipeline.

#### P2-3 — `_collect_features` non parallelizza, throttle fisso
- File: `src/scanner.py:123, :34`.
- Problema: 33+ asset × ~250 ms (get_prices + get_market) = ~16-17 s di wall time, sequenziali. Non è bloccante (cron 60-min ha tempo), ma allunga la finestra in cui il prezzo che il LLM vede è "vecchio".
- Raccomandazione: `concurrent.futures.ThreadPoolExecutor(max_workers=4)` con il rate limiter attualmente implicito. Capital ammette ~10 req/sec; 4 paralleli con `_FEATURE_REQUEST_DELAY_SEC` per worker stanno larghi.

#### P2-4 — `_apply_trailing_stop` fa import condizionale e lavoro duplicato
- File: `src/position_monitor.py:73, :481`.
- Problema: viene chiamato sia da `run_trailing_stops` (cron 5 min) sia da `monitor_positions` (cron 30 min). I due cron si sovrappongono ogni 30 min. Inoltre crea `Anthropic` ad-hoc dentro `_evaluate_position` (linea 332) per ogni posizione invece di riusarne uno.
- Raccomandazione: unificare in un solo `monitor_run(slim=True/False)` chiamato dal cron giusto; istanziare il client `Anthropic` una volta a inizio funzione.

#### P2-5 — Reconcile inline + cron orario fanno doppio lavoro
- File: `src/position_monitor.py:485`, `jobs/reconcile.py`, `deploy/crontab.txt:45`.
- Problema: il monitor 30-min chiama già `reconcile_open_trades` inline; il cron orario `15 * * * *` lo richiama. Logica idempotente, ma raddoppia chiamate `/history/activity` (anche se il 50% delle volte non chiude nulla) e i log sono duplicati.
- Raccomandazione: lasciare un solo punto. Se l'inline copre la finestra attiva, il cron orario può girare solo fuori orario `0 0,1,2,3,4,5,6,23 * * *`. In alternativa togliere l'inline e tenere il cron, è più semplice da debuggare.

#### P2-6 — `_check_entry_still_valid` usa il prezzo corrente dopo `login()` e prima di `create_position`, finestra di slippage
- File: `src/confirm_handler.py:537-554, :198`.
- Problema: tra il check R:R e l'effettiva `create_position` passano millisecondi, ma la `min_rr_at_entry` di default è 1.2: in mercati nervosi il sizing parte e il riempimento può degradare ulteriormente. La logica controlla R:R ma non slippage assoluto.
- Raccomandazione: aggiungere un `max_slippage_bps` (es. 25 bps) tra `signal.entry_price` e prezzo corrente, oltre al check R:R.

#### P2-7 — `LLMAnalyzer` valida solo proposals, ignora gli altri campi del JSON
- File: `src/llm_analyzer.py:212-228`.
- Problema: se il prompt evolve e il modello introduce campi (es. `market_regime`), oggi sono droppati silenziosamente. Manca un meccanismo per loggare campi imprevisti utile per evolvere il prompt.
- Raccomandazione: loggare le chiavi top-level del JSON quando differiscono da `{"proposals"}` per individuare drift.

#### P2-8 — Discovery share usa nodi hard-coded che possono cambiare
- File: `src/discovery.py:50`.
- Problema: `hierarchy_v1.shares.us.most_volatile` è un nodo opaco di Capital. Se Capital lo rinomina, la discovery share esce vuota e non c'è alert.
- Raccomandazione: contare le entry restituite e, se per N giorni di fila < soglia, inviare un alert Telegram una volta sola (debounce).

#### P2-9 — `executor._market_meta` dichiara `min_stop_distance` ma il default 0 disabilita silenziosamente la check
- File: `src/executor.py:79-110`.
- Problema: `_enforce_min_distance` rispetta la min distance del broker, ma se `dealingRules.minStopOrProfitDistance.value` torna come stringa con virgola decimale o non è presente, `float(0)` -> nessun enforcement; Capital risponde poi con 400 e si perde il signal.
- Raccomandazione: loggare warning quando la distance è zero/None invece di silenziarlo. Considerare un fallback ATR-based (es. min 1×ATR di stop) per asset volatili.

#### P2-10 — Schema DB: nessun indice su `signals.created_at` e `trades.signal_id`
- File: `supabase/migrations/20260417211133_init_schema.sql:58-60`.
- Problema: `idx_signals_status` è composto su (status, created_at desc); va bene per "pending recenti". Per `recent_signal_assets(hours=24)` (`db.py:140`) il filtro è solo su `created_at >= cutoff` e l'index parziale per status non viene usato. Stesso per `get_trade_by_deal_id` che usa unique constraint su `capital_deal_id` (ok), ma `trade.signal_id` è cercato implicitamente in `_maybe_build_rotation_proposal` via `db.get_trade_by_deal_id(...)` (ok) — invece la join logica sarebbe sull'unione signals/trades.
- Raccomandazione: aggiungere `create index idx_signals_created_at on signals(created_at desc);` e `create index idx_trades_signal_id on trades(signal_id);`. A bassissimo impatto storage, fanno spike i query plan.

### P3, nice-to-have

#### P3-1 — `MARGIN_BUDGET_EUR` deprecato ma ancora referenziato
- `src/config.py:104-107` accetta sia `EXPOSURE_BUDGET_EUR` sia `MARGIN_BUDGET_EUR`. I workflow `.github/workflows/*.yml` passano ancora `MARGIN_BUDGET_EUR`. Le docstring di `risk.py` parlano di "budget di margine"; la field è `exposure_budget_eur`.
- Raccomandazione: scegliere un nome (`exposure_budget_eur` è più corretto perché è quello che l'utente vede nei messaggi) e fare deprecation pulita: warning runtime se la vecchia env è settata, switch ai workflow al prossimo deploy.

#### P3-2 — `traceback.print_exc()` nei job, output non raggiungibile in cron
- `jobs/morning_scan.py:37`, simili negli altri entry point.
- Quando il fallback `TelegramClient.send_message` fallisce, il `traceback` finisce su stdout, che cron redirige in `logs/scan.log`: ok. In Actions invece è il job log GitHub. Funziona, ma è simmetrico: meglio `log.exception` con stesso effetto.

#### P3-3 — `Asset.asset_class` con tipi liberi mette in confusione
- `universe.py:18` dichiara `"metal" | "energy" | "index" | "fx"`; `discovery._INSTRUMENT_TYPE_TO_CLASS` produce `"crypto" | "share" | "fx" | "index" | "commodity"`. I valori non si sovrappongono ("metal" vs "commodity"). Il LLM riceve entrambe le forme nel campo `asset_class`.
- Raccomandazione: tassonomia unica con `Literal`, mappare entrambe le sorgenti.

#### P3-4 — `feedparser.parse` blocca senza timeout esplicito sul socket
- `src/news.py:113`. La funzione passa `request_headers` ma `feedparser` internamente usa `urllib`, niente timeout. Un feed RSS lento potrebbe stallare il job.
- Raccomandazione: passare via `requests.get(..., timeout=10)` e poi `feedparser.parse(response.text)`.

#### P3-5 — `Database` non ha contesto/cleanup; il client supabase mantiene aperto un pool
- `src/db.py:30`. Per la cadenza attuale non è un problema. Se in futuro si vorrà eseguire più scan in un singolo processo (es. backtester), serve un `close()` o context manager.

#### P3-6 — `event_extractor.py`, `news_analyzer.py`, `briefing.py`, `weekly_report.py` non sono stati ispezionati nel dettaglio
- Sono stati letti solo per farsi un quadro: il briefing è disabilitato, weekly report è cron domenicale e il news_analyzer è dipendente dal briefing. Vale la pena un giro dedicato quando si riabiliteranno i briefing.

## 3. Quick wins (≤ 1 giornata)

1. Aggiungere `SUPABASE_SERVICE_ROLE_KEY` ai 4 workflow Actions (P0-1). 5 minuti, sblocca il fallback.
2. Abilitare prompt caching Anthropic su `LLMAnalyzer.rank_setups` e su `_evaluate_position` (P1-2). Mezza giornata se si vogliono anche logging dei `cache_read_input_tokens`.
3. Indici Postgres `idx_signals_created_at` e `idx_trades_signal_id` (P2-10). Nuova migration, 10 minuti.
4. Rimuovere il trailing inline da `monitor_positions` (P0-2 punto 1), tenendolo solo nel cron 5-min. Cambio di poche righe in `position_monitor.py:508-515`.
5. Aggiungere `pytest` al `requirements.txt` con 6-8 unit test sui moduli puri (`risk`, `quiet_hours`, `macro_guard`, `reconcile._extract_close_info`). Nessuna dipendenza Capital. Mezza giornata.
6. Logging del `usage` di ogni chiamata Anthropic (`response.usage.input_tokens`, `cache_read_input_tokens`, `output_tokens`) per validare a posteriori P1-2 e tunare cap del pre-filter.
7. Allineare workflow `.yml` da `MARGIN_BUDGET_EUR` a `EXPOSURE_BUDGET_EUR` (P3-1). 5 minuti.

## 4. Refactor strutturali (> 1 giornata)

1. **Sessione Capital condivisa**: estrarre un `CapitalSession` da iniettare nei flussi monitor/scanner/confirm-handler. Risolve P0-3 e P0-2 punto 3, prerequisito per i lock applicativi su `update_position`.
2. **Spezzare `scanner.py`**: `scanner_orchestrator.py` (run_morning_scan), `scanner_messages.py` (formatter Telegram), `scanner_rotation.py` (rotation logic). Permette di testare unit la parte di rotation senza istanziare Capital. P2-1.
3. **Schema validation strutturato per output LLM**: passare a `tool_use` o pydantic per `SetupProposal` e `MonitorDecision`, con retry su parse error. P1-3. Una giornata abbondante se si vogliono coprire entrambi i call site.
4. **Listener daemon assorbe `manage_positions`**: oggi è una funzione "polling sincrono" che vive su VM ma ruba update al daemon. Convertendola a "manda messaggio con bottoni `close:<deal_id>`, gestisci i click in `confirm_handler`" si elimina una fonte di concorrenza Telegram. P0-2 punto 2.
5. **Tassonomia asset unica**: `Asset` dataclass con tutti i metadati (universe, weekend allowlist, macro group, keywords); le strutture derivate vengono generate da quella. P1-6 e P3-3. Una giornata.
6. **Test integration con VCR**: registrare risposte Capital reali una volta sola e replayare per testare `compute_features`, `executor`, `reconcile` end-to-end senza mock manuali. Forte ROI per il futuro, 2-3 giornate.

## 5. Domande aperte / aree dove serve più contesto

1. **Il workflow Actions è davvero un fallback usato?** Tutti gli `on:` sono `workflow_dispatch`. Se sì, P0-1 è bloccante; se no, vale comunque sistemare la coerenza per evitare sorprese future.
2. **Lock applicativo: VM-only o multi-host?** La proposta `pg_advisory_lock` cambia se in futuro si vorrà attivare sia VM sia Actions per ridondanza.
3. **Costo target Anthropic per giorno?** Non sono presenti budget hard nel codice. Sapere il target permette di calibrare prompt caching, dimensione `max_candidates` (oggi 8), max_tokens (2000 e 400) e cadenza monitor (oggi 30 min con LLM).
4. **`min_rr_at_entry=1.2` è una scelta consapevole o un default storico?** È molto permissivo; in mercati con spread alto degrada il PnL atteso anche su signal originariamente buoni.
5. **Retention Supabase**: `monitoring_events` e `scanner_runs` crescono ogni 30/60 min. Su Supabase free tier (500 MB) il margine è ampio ma serve un job di pruning. Esiste una policy condivisa o aspettiamo che sia un problema?
6. **Stato delle 4 alt-coin non-major** (`Ethereum Classic`, `EthereumFi`, `EthereumPoW`, `ARPA`): se l'analisi storica del 2026-04-25 ha già escluso le micro-cap nel weekend, valutare se rimuoverle anche dall'universo settimanale o se servono come "controllo" per validare il filtro.
7. **Modello Anthropic differenziato**: oggi `ANTHROPIC_MODEL` e `ANTHROPIC_MODEL_FAST` puntano allo stesso `claude-haiku-4-5-20251001`. Il costo è già al minimo. Se il prompt caching abbassa il costo del ranker, ha senso provare Sonnet 4.6 sul ranker (qualità) e tenere Haiku sul monitor?

## Cose che funzionano bene, da non rompere

- Pre-filter deterministico + safety net + cap (`scanner._prefilter_candidates`): pulito, log dei counters molto utile per tunare a posteriori.
- Allowlist weekend + filtro blow-off (`scanner.WEEKEND_CRYPTO_MAJOR_ALLOWLIST`): risposta corretta a un problema empirico documentato.
- Reconcile resiliente con calcolo PnL fallback (`reconcile._extract_close_info`): gestisce bene il caso in cui Capital non restituisce `amount`.
- `quiet_hours` con weekday/weekend differenziati e gestione DST via `ZoneInfo`: scelta giusta.
- Listener systemd con `Restart=always`: pattern corretto per il long-poll Telegram, niente costi Actions inutili.
- Gestione retry login Capital con backoff (`capital_client.login`): ben pensata, distingue tra 429 e errore di rete.
- Idempotenza dello `setup_vm.sh`: un comando per portare la VM in stato corrente.
