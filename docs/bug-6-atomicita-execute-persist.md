# Bug #6 — Atomicità execute → persist

Stato: indagine completa, fix non implementato (in attesa di conferma).

## 1. Sintesi

Quando l'auto-executor del segnale apre la posizione su Capital ma la persistenza in `trades` fallisce, il signal viene marcato `cancelled_other` e la posizione su Capital resta orfana. Il `position_monitor` la importa pochi minuti dopo come orphan (`signal_id=NULL`), perdendo per sempre il link `signal → trade → outcome`. Il record `features_at_decision` del signal non è più raggiungibile dal trade: questo sample è contaminato per la validazione hit-rate per score.

## 2. Conferma diagnosi dai log VM

Log estratti da `~/tradealert/logs/scan.log` su `ubuntu@92.4.165.254` (i timestamp dei log sono Europe/Rome, +2h rispetto allo store DB in UTC).

Caso di riferimento: **signal #80 → orphan trade #33**, 14/05/2026.

```
2026-05-14 20:05:32  Signal salvato id=80
2026-05-14 20:05:32  Signal 80 in finestra auto-confirm (60s), polling ogni 5s
2026-05-14 20:06:33  Signal 80: timeout finestra, apertura automatica
2026-05-14 20:06:33  GET trades?select=pnl&status=eq.closed   (weekly_realized_pnl)
2026-05-14 20:06:34  POST trades   HTTP/2 409 Conflict
2026-05-14 20:06:34  ERROR src.scanner: Auto-execute crash su signal 80
  postgrest.exceptions.APIError:
    'duplicate key value violates unique constraint "trades_capital_deal_id_key"'
    Key (capital_deal_id)=(00384627-0001-54c4-0000-0000891a7cf0) already exists.
2026-05-14 20:06:34  PATCH signals?id=eq.80   (status=cancelled_other)
2026-05-14 20:10:03  WARNING src.position_monitor:
  Posizione manuale 00384627-0001-54c4-0000-0000891d224a (US Tech 100)
  importata in trades come orphan
```

Punti notevoli:

1. L'eccezione che fa scattare `cancelled_other` è proprio dentro `db.insert_trade()` (executor.py:334), catturata dal try/except di `scanner.py:1140-1151`.
2. Il `deal_id` in conflitto (`891a7cf0`) non era quello appena aperto su Capital: era il deal_id del trade #31 (Nasdaq 100, signal #77), aperto il giorno prima. Quindi al crash di atomicità si è sommato un **bug di selezione** nel match indiretto `epic+direction+size` dell'executor.
3. Il bug di selezione è stato già mitigato dall'utente col commit `2557189` ("Executor: match posizione esclude deal_id gia' tracciati"), datato 14/05 20:18:54 — 12 minuti dopo l'incidente. La mitigazione filtra via i `dealId` già presenti in `trades` ma **non risolve** il bug #6: qualunque eccezione fra `create_position` e `insert_trade` (rete, RLS, schema diff, vincoli) lascia comunque la posizione orfana.
4. Activity history Capital del 14/05 18:06:34 UTC restituisce per `dealId 891d224a` (il deal vero, non quello duplicato): `epic=US100`, `direction=BUY`, `size=0.01`, `level=29554.9`, `dealReference=p_00384627...891d224a`. **Tutti i campi necessari per il backfill sono esposti**.

## 3. Mappa dei punti non-atomici

Il path completo `signal pending → trade row con signal_id` ha 11 step ordinati. Ogni eccezione fra lo step 6 (incluso) e il completamento dello step 10 lascia la posizione aperta su Capital senza riga `trades` corrispondente.

Riferimento codice: `src/scanner.py:_handle_confirm` per il wrapper, `src/executor.py:execute_signal` per gli step 1-10.

| # | Step | File:line | Cosa lascia se fallisce |
|---|---|---|---|
| 1 | get_open_positions (limite) | executor.py:139 | nessun effetto |
| 2 | get_market + leverages + meta | executor.py:147-149 | nessun effetto |
| 3 | get_account_info per available_margin | executor.py:163 | nessun effetto |
| 4 | calculate_size | executor.py:174 | nessun effetto |
| 5 | _enforce_min_distance | executor.py:199 | nessun effetto |
| **6** | **create_position su Capital** | executor.py:210 | **se 200 ma TCP rotto prima del JSON → orfano lato broker, nessuna traccia DB** |
| 7 | confirm_deal | executor.py:236 | gestito con `confirm_failed=True` + fallback su get_open_positions; ma se anche il fallback fallisce → orfano |
| **8** | get_open_positions fallback (3 tentativi) | executor.py:280 | se Capital risponde con lista parziale o latency > finestra: `matched_position=None` |
| 9 | aggiornamento `deal_id`/`fill_level`/`stop_level`/`profit_level` da matched_position | executor.py:321-331 | nessun effetto |
| **10** | **db.insert_trade** | executor.py:334 | **causa concreta osservata il 14/05: orfano** |
| 11 | db.update_signal_status('executed') + insert_monitoring_event | executor.py:347-360 | se 11 fallisce dopo 10: trade nel DB ma signal incoerente (minore impatto, recuperabile) |

Modi di failure parziale non coperti:

- Capital risponde 200 a `create_position` ma il client `requests` non legge il body (rete, kill -9, timeout fra `send` e `recv`). Senza idempotency key, un retry apre una seconda posizione.
- `confirm_deal` torna `dealStatus=ACCEPTED` ma `affectedDeals` vuoto → `deal_id=None`, poi `matched_position` sostituisce con un deal_id sbagliato (è quello che è successo prima del commit `2557189`).
- `db.insert_trade` fallisce per network transient → executor solleva, lo scanner marca `cancelled_other`, e il segnale viene perso anche se la posizione esiste e la riga `trades` sarebbe stata corretta. Nessun retry.
- `update_signal_status` fallisce dopo insert_trade riuscito → trade orfano in senso opposto (link ok, signal status incoerente). Caso minore ma da tracciare.

Una versione "rinforzata" via solo retry non basta: serve un meccanismo di **transizione di stato persistito** (vedi proposta).

## 4. Proposta di fix

Tre tier di intervento, indipendenti, applicabili in ordine.

### Tier 1 — Recovery sicuro (priorità alta, basso rischio)

Obiettivo: smettere di perdere il link `signal → trade` quando arriva un'eccezione dopo `create_position`. Recuperabile, niente nuove tabelle.

1. **Nuovo status segnale**: `execute_inconsistent` (oppure `executing_capital_unknown`).
   In `scanner.py:1144`, sostituire `cancelled_other` con `execute_inconsistent` **solo nel ramo dell'except**, perché qui l'eccezione arriva dopo l'inizio di `execute_signal` e non sappiamo a che step si è rotto. Le altre marcature `cancelled_other` (riga 1127, market_status non TRADEABLE) restano com'è.

2. **Reconcile-aware orphan adoption** in `position_monitor.py:151-189`.
   Prima di creare l'orphan trade con `signal_id=None`, cercare un signal con:
   - `status='execute_inconsistent'`
   - finestra temporale `created_at` fra (orphan position `createdDate` − 10min) e (now)
   - `epic` corrispondente all'`epic` della posizione Capital (via `asset_features.epic` salvato in `features_at_decision`, già disponibile)
   - `direction` corrispondente
   - tolleranza opzionale su size

   Se match unico → ricostruisci il link: `insert_trade(signal_id=match.id, ...)` + `update_signal_status(match.id, 'executed_recovered')`.
   Se nessun match o più match ambigui → fallback al comportamento attuale (orphan + `Telegram WARNING`).

3. **Notifica esplicita**: la sezione attuale del monitor logga solo `WARNING`. Aggiungere un `telegram.send_message()` quando viene adottato un orphan, distinguendo i due casi (recovered vs vero orphan). Senza questo, oggi gli incidenti come quello del 14/05 si scoprono "per caso" dal weekly report.

Note di rollout:
- L'enum `signals.status` se è un check constraint Postgres va aggiornato con migrazione Supabase.
- Il reconcile (`jobs/reconcile.py`) attualmente non gestisce signal: è il `position_monitor` la sede naturale, perché è lui che già fa orphan import. Non aggiungere nuovi job.

### Tier 2 — Prevenzione: pre-commit del link (priorità media, rischio moderato)

Obiettivo: il link `signal → trade` esiste **prima** della chiamata broker, così non dipende dalla sopravvivenza dell'executor.

Due opzioni, da scegliere:

**Opzione A — `trades.status='pending_capital'`** (più invasiva, più pulita):
- Prima di `create_position`: `insert_trade({signal_id, epic, direction, size, status='pending_capital', capital_deal_id=NULL, ...})` con tutti i dati derivabili. `capital_deal_id` nullable.
- Dopo `create_position` success: `update trade set capital_deal_id=..., entry_price=fill_level, status='open'`.
- Su failure: `update trade set status='failed'` con `exit_reason` testuale.
- Vincolo `trades_capital_deal_id_key` va modificato in `unique nulls distinct` o `unique partial where status!='pending_capital'`.

**Opzione B — Tabella separata `signal_to_trade_link`** (meno invasiva):
- Nuova tabella `(signal_id PK, attempted_at, status text, capital_deal_id text nullable, error_text text nullable)`.
- Step 6 dell'executor diventa: `insert(signal_to_trade_link, status='attempting')` → `create_position` → `update status='capital_open', capital_deal_id=...` → `insert_trade(...)` → `update status='persisted'`.
- Reconcile/orphan importer interrogano sempre questa tabella prima di creare orphan. Stesso `signal_id` rivelato anche se `trades` non è stato scritto.
- Vantaggio: nessuna modifica alla tabella `trades` né ai consumer downstream (performance.py, weekly_report.py, scanner.py).

Raccomandazione: **Opzione B**. Il blast radius su `trades` è alto (`performance.py` aggrega su `status='closed'`, `db.get_open_trades()` su `status='open'`, varie query filtrano per status implicitamente). Una tabella ancillare lascia invariata la semantica esistente e dà allo stesso tempo il marker pre-broker che serve.

### Tier 3 — Observability (priorità bassa, rischio nullo)

1. Alert Telegram quando il `position_monitor` adotta un orphan (vedi Tier 1.3, ridondante se quello è fatto).
2. Counter cumulativo orfani / settimana, esposto nel `weekly_report.py` come riga `Posizioni orphan adoptate: N`.
3. Loggare strutturato (`extra={...}`) gli step 6/10 con `deal_reference`, `signal_id`, esito: utile per grep retroattivo. Costo zero.

## 5. Backfill retroattivo del trade #33

Fattibile e a basso rischio. Activity history Capital (verificato sulla VM, env=live):

```
{date: '2026-05-14T20:06:34.280', type: POSITION, status: ACCEPTED,
 epic: US100, dealId: 00384627-0001-54c4-0000-0000891d224a,
 details: {dealReference: 'p_00384627-...-891d224a',
           level: 29554.9, direction: BUY, size: 0.01}}
```

Match con signal #80 (US100, long, 14/05 18:05:32 UTC, ±60s dall'apertura ACCEPTED del 14/05 18:06:34 UTC) è univoco. Un job di backfill `jobs/backfill_orphan_links.py` può:

1. Selezionare orphan: `trades.status in ('open','closed') and signal_id is null and exit_reason like 'manual_import:%'`.
2. Per ognuno, query Capital `get_activity_history` finestra ±5min attorno a `opened_at`, match su `dealId == trade.capital_deal_id`.
3. Estrarre `date` ACCEPTED → cercare in `signals` un record con `epic=epic_della_posizione`, `direction=match`, `status in ('cancelled_other','execute_inconsistent')`, `created_at` in finestra ±5min.
4. Se match unico → `update trades set signal_id=...`, log audit.

Per il caso specifico del 14/05 il backfill ricucirebbe `#33 → #80` e salverebbe `features_at_decision` del signal nel training set.

## 6. Domanda aperta da Andrea

> Se Capital ha `dealReference` ma noi non lo abbiamo persistito, possiamo recuperarlo via activity history filtrando per timestamp ±60s e epic? Permetterebbe a un job di backfill di ricucire #33 → #80 retroattivamente.

Risposta: **sì**, confermato dal test sulla VM. `get_activity_history` espone `dealId`, `details.dealReference`, `details.level`, `details.direction`, `details.size`, oltre a `epic` e `date` (UTC). Una finestra ±60s su `epic+direction+size` è praticamente sempre univoca su questo bot (max 1-2 segnali/ora sullo stesso epic). Se non lo fosse, si stringe a ±15s o si chiede conferma utente via Telegram.

## 7. Cosa NON è oggetto di questo bug

- Il "bug di selezione" `matched_position` (deal_id sbagliato preso dal match indiretto) è stato già patchato dal commit `2557189`. Va comunque testato in regressione: il filtro `known_deal_ids` dipende da `db.get_open_trades()` che potrebbe fallire silenziosamente (vedi executor.py:271-273 dove l'except mette `known_deal_ids=set()`). In quel caso il bug ripartirebbe. Vale la pena alzare il livello del log da `warning` a `error` su quel branch.
- La race fra `auto-confirm window timeout` e `manual skip` da Telegram è già coperta dal re-fetch in scanner.py:1083.

## 8. Decisioni e workflow concordati

1. Tre branch separati: A) Tier 1 + Tier 3 + fix log `known_deal_ids`; B) Tier 2 con tabella ancillare; C) backfill orphan.
2. Pre-commit: Opzione B (tabella `signal_to_trade_link`).
3. Backfill: job dedicato `jobs/backfill_orphan_signal_links.py`. Match ambiguo → log + skip (niente conferma Telegram).
4. Migrazione enum `signals.status`: due valori, `execute_inconsistent` e `executed_recovered`, tenuti separati. `executed_recovered` marca i sample con asterisco metodologico (link ricostruito a posteriori).

## 9. Addendum — scenario residuo dopo Tier 1 + fix log + commit 2557189

Catena di failure ricostruita:

```
(a) db.get_open_trades() solleva       (network/RLS/timeout)
(b) executor.py: known_deal_ids = set() (log ora ERROR + Telegram)
(c) match indiretto epic+dir+size aggancia un deal_id gia' in trades
(d) db.insert_trade -> 409 duplicate key
(e) scanner.py except: signal.status = 'execute_inconsistent'
(f) position_monitor (ciclo successivo, ogni 30min):
    - find_inconsistent_signal(epic, direction, window=10min)
    - match unico -> insert_trade(signal_id=#80, ...) + status='executed_recovered'
    - monitoring_event 'orphan_adopted_recovered' con latency_seconds
    - Telegram notice "Recovery posizione"
```

**Il sample resta valido**. Tutti i metadati del signal restano integri:

- `features_at_decision` (jsonb) → scritto da `insert_signal` al momento del salvataggio iniziale (scanner.py:7028 nel run del 14/05), il fallimento successivo non lo tocca.
- `thesis`, `score`, `epic`, `direction`, `stop_loss`, `take_profit` → idem.
- L'unico campo che il recovery scrive è `signals.status` (da `execute_inconsistent` a `executed_recovered`). Niente perdita.

`executed_recovered` è tenuto distinto da `auto_confirmed` per dare alla validazione un filtro esplicito: chi vuole essere conservativo include solo `auto_confirmed`/`executed`/`manual_skipped`; chi vuole il campione esteso include anche `executed_recovered`.

**Latency di ricostruzione**. Salvata in `monitoring_events.details.latency_seconds` per l'evento `orphan_adopted_recovered`. Definizione: `now() - signal.created_at` al momento dell'adoption. Range atteso:

- Limite inferiore: ~5s se l'incidente accade subito dopo il timeout finestra auto-confirm e il monitor passa "vicino".
- Tipico: **2-30 minuti**, dominato dalla frequenza del cron `position_monitor` (30min in orario mercato).
- Limite massimo prima di rinunciare al recovery: **10 minuti** (parametro `window_minutes` di `find_inconsistent_signal`). Oltre, il signal non viene più considerato candidato e la posizione resta orphan. Soglia conservativa per ridurre falsi positivi su signal vecchi con stesso epic+direction.

Il campo è queryable in SQL: `select details->>'latency_seconds' from monitoring_events where event_type='orphan_adopted_recovered'`. Distribuzione disponibile per tarare `window_minutes` in seguito.

**Sentinella**. Il `weekly_report.py` espone `Orphan adottati: N (di cui recovered: M)` ogni domenica. Se `N-M > 0` e l'utente non sta aprendo posizioni manualmente, vuol dire che il monitor sta importando posizioni che il recovery non ha catturato: o l'eccezione executor avviene prima che il signal venga salvato (così nessun candidate esiste), oppure la `window_minutes` è troppo stretta. Da indagare appena succede.

## 10. Cosa lascia ancora scoperto Tier 1

Tier 1 cattura **solo** il sotto-caso "Capital aperta + insert_trade fallito + signal già nel DB con status valido per il recovery". Restano scoperti:

- **Pre-signal-save**: eccezione tra LLM response e `insert_signal`. Niente signal nel DB → niente candidate → orphan classico. Probabilità bassa (è praticamente solo `insert_signal`), ma esiste.
- **Capital response 200 ma TCP rotto prima di leggere `dealReference`**: l'executor non sa cosa è successo, e un retry aprirebbe una seconda posizione. Idempotency key non disponibile su Capital. Resta scoperto. Mitigazione futura: introdurre un `client_request_id` salvato pre-chiamata e riconciliato via activity history (Tier 2 lo permette).
- **Recovery oltre i 10 minuti**: cron `position_monitor` può saltare un ciclo se il job precedente è long-running. Mitigazione: ridurre a `*/15` (oggi `*/30`) o allungare `window_minutes` dopo aver visto la distribuzione reale di `latency_seconds`.

Questi tre punti li chiude Tier 2 (pre-commit del link prima della chiamata broker). Per ora, Tier 1 risolve il caso osservato e abilita la sentinella nel weekly report.
