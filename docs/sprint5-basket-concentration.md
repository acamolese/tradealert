# Sprint 5 — Espansione paniere + tetto di concentrazione (analisi + fattibilità)

Data: 2026-06-21. Giro di **analisi + verifica fattibilità + proposta**, NESSUN
deploy. Verifiche live su Capital (sola lettura) + lettura codice. Da leggere
insieme prima di accendere qualunque flag.

**Sintesi in una riga**: gli FX sono fattibili **solo per le coppie quotate in
USD** (EUR/USD, GBP/USD, AUD/USD); **USD/JPY è BLOCCATO** da un bug di sizing
non-currency-aware (verrebbe rifiutato a ogni scan). Il tetto di concentrazione
B1/B2 è pulito da implementare e non sfiora V1. Primo blocco proposto: **+3 FX
USD → totale 8 asset**, non 10 (solo 3 diversificatori veri esistono).

---

## PARTE 0 — Fattibilità FX su Capital (bloccante)

### D0.1 — Epic/ticker ✅

Tutti validi e di tipo **CURRENCIES** (spot FX, non future/opzioni), già mappati
in `src/watchlist.py` (non nell'universo attivo). Verificato live:

| candidato | epic | type | spread % (weekend*) |
|-----------|------|------|---------------------|
| EUR/USD | `EURUSD` | CURRENCIES | 0.044 |
| USD/JPY | `USDJPY` | CURRENCIES | 0.056 |
| GBP/USD | `GBPUSD` | CURRENCIES | 0.128 |
| AUD/USD | `AUDUSD` | CURRENCIES | 0.071 |
| USD/CHF | `USDCHF` | CURRENCIES | 0.212 |

`*` Misurati a mercato **CHIUSO** (weekend): gli spread reali in sessione attiva
sono più stretti. Da ri-misurare in sessione prima del deploy (vedi A1).

### D0.2 — Candele ✅

5m disponibili per tutti (293-295 candele su 3 giorni), come Brent/Gold; fallback
15m presente. **Gap weekend FX** (ven sera → dom sera): identico per natura a
quello che il sistema **già gestisce** per le commodity, anch'esse CLOSED nel
weekend (verificato: oggi è weekend, Brent/Gold/FX tutti CLOSED, solo BTC
tradeable). Nessuna logica nuova richiesta: lo scanner gira 07-22 Roma e salta
gli asset CLOSED come fa già.

### D0.3 — Sizing (IL PUNTO CRITICO) ⚠️ — bug parziale isolato

La logica di sizing (`src/risk.py`) calcola il rischio come
`risk_per_unit = entry_price × stop_pct/100`, cioè in **unità di prezzo (valuta
quotata)**, e lo confronta col cap in EUR **senza conversione di valuta**. Questo
è corretto solo se la valuta quotata ≈ EUR come ordine di grandezza. Confronto 1R
reale (budget 15€, cap 5€, stop 0.5%):

| epic | entry | risk/unit calcolato | valuta quotata | size | risk_est codice | esito |
|------|-------|---------------------|----------------|------|-----------------|-------|
| EUR/USD | 1.1476 | 0.00574 | USD | 300 | 1.72 € | ✅ ok |
| GBP/USD | 1.3236 | 0.00662 | USD | 300 | 1.99 € | ✅ ok |
| Brent | 79.99 | 0.39995 | USD | 3.7 | 1.48 € | ✅ (baseline) |
| **USD/JPY** | 161.31 | 0.80655 | **JPY** | **None** | **80.65 €** | ❌ **RIFIUTATO** |

**Cosa succede:** per le coppie quotate **USD** (EUR/USD, GBP/USD, AUD/USD) il
sizing funziona, con un errore **conservativo del ~13-15%**: il rischio è
calcolato in USD ma trattato come EUR, e siccome 1 USD < 1 EUR il rischio reale
in EUR è leggermente **inferiore** al cap (es. EUR/USD risk_est 1.72 "€" ≈ 1.50 €
reali). Tollerabile e nel verso giusto (sotto-rischia, non sovra-rischia).

Per **USD/JPY** il sizing **si rompe**: la valuta quotata è JPY, e
`risk_per_unit = 161.31 × 0.5% = 0.807 JPY` viene trattato come **0.807 EUR**.
Il rischio reale è 0.807 JPY ≈ **0.0047 €/unità** (fattore ~175), ma il codice lo
crede 175× più grande, calcola un rischio di **80.65 € sulla size minima** > cap
5 €, e **scarta il trade come "non eseguibile entro risk cap" a ogni scan**.
Non è pericoloso (non apre una posizione mal dimensionata, la rifiuta), ma USD/JPY
**non aprirebbe mai**.

**Decisione (stop-and-flag, come richiesto):** USD/JPY **ESCLUSO** finché il
sizing non diventa currency-aware (conversione quote→EUR via tasso, fix separato
da pre-registrare). EUR/USD, GBP/USD, AUD/USD procedono con l'errore conservativo
~15% accettato e documentato. USD/CHF: quotata CHF ≈ parità con EUR, sizing ok,
ma spread alto (0.21%) → parcheggiata (vedi A1).

### D0.4 — Feature ✅ (con un caveat per A3)

`compute_features` gira e produce valori sensati su FX:

| epic | rsi | slope | atr% | bb% | pct_high20 |
|------|-----|-------|------|-----|------------|
| EUR/USD | 28.1 | −0.082 | **0.266** | 2.17 | −1.16 |
| USD/JPY | 79.3 | 0.043 | **0.197** | 1.11 | −0.08 |
| Brent | 54.3 | 0.081 | **1.494** | 3.95 | −0.35 |

RSI, slope, posizione-nel-range in range normali. **Ma l'ATR% FX è ~6× più basso**
di Brent (0.2-0.27 vs 1.5): gli FX major sono strutturalmente meno volatili. Le
feature sono calcolabili e sensate, ma questa differenza di scala alimenta il
dubbio A3 (coerenza scoring cross-classe) — vedi sotto.

### Verdetto Parte 0

FX **fattibili per le major USD-quoted** (EUR/USD, GBP/USD, AUD/USD). **USD/JPY
bloccato** (sizing). USD/CHF tecnicamente ok ma spread alto. Nessun ripiego su
indici esteri necessario: 3 diversificatori veri esistono già.

---

## PARTE A — Espansione paniere

### A1 — Lista finale primo blocco + diversificazione

Criterio chiave (driver diverso): gli FX major hanno driver **valutari/tassi**,
scorrelati da commodity (Gold/Brent), equity (US500/Nasdaq), crypto (BTC). Aggiungere
ETH/Silver/altri indici **concentrerebbe** (tutto risk-on/risk-off insieme),
peggiorando ciò che il tetto deve contenere → esclusi per principio.

**Spread/ATR (l'erosione del momentum-follower):** il punto delicato. Spread come
frazione di 1 ATR (più alto = peggio):

| asset | spread% (weekend) | ATR% | spread/ATR |
|-------|-------------------|------|------------|
| Brent | 0.025 | 1.49 | **0.017** |
| EUR/USD | 0.044 | 0.27 | **0.165** |
| GBP/USD | 0.128 | ~0.3 | ~0.43 |
| AUD/USD | 0.071 | ~0.3 | ~0.24 |

Gli FX hanno spread/ATR **~10-25× peggiore** di Brent: lo spread mangia una quota
ben maggiore del movimento atteso. **MA**: questi spread sono da mercato chiuso
(gonfiati). In sessione attiva EUR/USD scende tipicamente a ~0.01-0.02%. Va
**ri-misurato in sessione** prima di confermare GBP/USD (0.128% weekend è al limite).

**Lista proposta primo blocco (da confermare dopo ri-misura spread in sessione):**

1. **EUR/USD** — il più liquido, spread minore, diversificatore primario.
2. **AUD/USD** — driver risk/commodity-currency, spread ok, scorrelato dal resto.
3. **GBP/USD** — driver GBP/tassi UK; **condizionato** a spread in sessione ≤ ~0.04%.

Totale: **5 attuali + 3 FX = 8 asset** (non 10: solo 3 diversificatori veri
esistono; USD/JPY bloccato, USD/CHF spread alto). Onestà: è meglio 8 puliti che
10 forzando dentro asset correlati o l'USD/JPY rotto.

### A2 — Costo proiettato

Il caching abbatte la dipendenza dal numero di asset: il system prompt (5028 token,
cache 1h, hit 91%) è **condiviso e cachato**; solo le feature dei candidati nel
user message scalano col paniere, ed è la parte piccola. Stima:

- Oggi (5 asset): ~$0,023/scan → ~$11/mese (16 scan/giorno × 30).
- 8 asset (+60% payload feature, ma su un input già piccolo): ~$0,027-0,030/scan
  → **~$13-14/mese**.

Delta **~+$2-3/mese**, ampiamente coperto dal margine creato dalla two-call
(−34% sul costo/scan, $5-6/mese risparmiati). **Logging:** il job `cost_report`
(lun 08:00) già traccia costo/scan reale; dopo 3-4 giorni col paniere allargato
confronta col proiettato qui. Predisposto, nessun lavoro aggiuntivo.

### A3 — Coerenza scoring cross-classe ⚠️ (da verificare forward)

Dubbio reale, **non risolvibile a priori**. Gli FX hanno ATR% ~6× più basso. Due
scenari: (a) lo score Sonnet valuta la *qualità del setup tecnico* (allineamento
trend/RSI/posizione), non la volatilità assoluta → 7.0 significa lo stesso
cross-classe; (b) i range diversi delle feature spostano sistematicamente lo score
FX in alto/basso. Non lo sappiamo finché non gira. **Predizione da verificare:** nei
primi giorni misurare **tasso di apertura e distribuzione score per classe**; se gli
FX aprono molto più/meno spesso del 9-10% baseline, la soglia 7.0 non è
cross-classe e va ricalibrata (o segmentata per classe). Lo aggancio al
monitoraggio esistente, non serve codice nuovo.

### A4 — Staged: criterio di allargamento

Primo blocco = **+3 FX (totale 8)**, NON big-bang. Criterio per allargare oltre
(es. USD/CHF, un indice estero a basso spread tipo DAX), da valutare **solo dopo
≥1 settimana** di dati, **se e solo se** tutte e tre reggono: (1) costo/scan ≤
proiezione, (2) score-per-classe sano (A3), (3) gli FX generano effettivamente
trade (non solo no_setup). Se gli FX restano muti o scorano fuori scala, si
ferma e si ricalibra prima di aggiungere altro.

**Caveat di merito (non bloccante):** gli FX major sono spesso più
mean-reverting delle commodity in fase direzionale (EUR/USD oscilla a lungo in
range). Il momentum-follower può accumulare falsi segnali. Non un motivo di
esclusione (ottimi diversificatori), ma nei primi giorni vanno guardati come dato
a sé, **non attesi a comportarsi come il Gold**.

---

## PARTE B — Tetto di concentrazione statico

### B1 — Anti-duplicato (asset, direzione) — principale

**Oggi:** il dedup vive in `src/scanner.py:1648` come
`db.recent_signal_assets(hours=24)` → esclude per **asset soltanto**, con finestra
**24h**. È ciò che ha lasciato passare #69/#71 (stesso Bitcoin long a 2 giorni:
la finestra 24h era scaduta).

**Estensione (pulita):** aggiungere un blocco "esiste già una posizione **aperta**
su quell'(asset, direzione)", indipendente dalla finestra temporale. Le posizioni
aperte sono **già recuperate** nello scanner (`capital.get_open_positions()`),
quindi il match (asset, direzione) vs trade aperti è diretto, non serve nuova
query. Convive col dedup-24h esistente (l'uno guarda i signal recenti, l'altro le
posizioni vive). **Logga ogni blocco** (setup bloccato + id posizione che blocca).
Sul caso #69/#71: con questa regola, #71 sarebbe stato bloccato (#69 ancora aperto).

### B2 — Tetto globale

Cap su posizioni aperte simultanee. **Nota:** `MAX_OPEN_POSITIONS` esiste già
(default 1 in config, ma impostato più alto nel `.env` VM — sono state viste 2
posizioni insieme). La proposta **6** va confrontata col sizing reale: 1R ≈ 5 €,
margine/trade ~11-30 €; 6 posizioni → margine bloccato ~70-180 €, rischio se tutte
colpiscono lo SL ~30 €. **Ragionevole se** l'equity/margin budget del conto lo
regge (da confermare col valore reale di `MARGIN_BUDGET_EUR` e l'equity). Se il
conto è piccolo, **4-5** è più prudente. **Comportamento setup bloccato dal cap:
scartato, niente coda** (concordo: semplicità, e coerente col comportamento attuale
"slot pieni → silenzio"). Logga ogni blocco.

**D-B2 risposta:** propongo di **riusare/estendere `MAX_OPEN_POSITIONS`** invece di
un secondo flag, settandolo a 6 (o 5) nel `.env`. Da fissare insieme dopo aver
guardato equity e margin budget reali.

### B3 — Cap per direzione (opzionale, OFF di default) ✅

Confermo: **implementato ma OFF**, configurabile (es. `MAX_OPEN_PER_DIRECTION`,
default 0 = disattivato). Cattura parzialmente "stessa direzione macro" (Gold+Nasdaq
short insieme) ma è crudo: troncherebbe un periodo legittimamente tutto-ribassista.
Da valutare **solo dopo** i dati del paniere. Lasciato spento.

### B4 — Verifica non-contaminazione V1 (obbligatoria) ✅

Confermo esplicitamente i tre punti:

- **(a) V1 continua a loggare il controfattuale D su tutti i trade aperti.** Paniere
  e tetto agiscono in `scanner.py` (selezione + apertura); V1 vive in
  `position_monitor._apply_trailing_stop` (trailing). Sono file e momenti diversi.
  Qualunque trade si apra, passa per lo stesso trailing che logga `offset_r_d`. ✅
- **(b) Il gate V1 resta sui campioni fascia bassa a prescindere da quali correlati
  il tetto blocca.** Il gate misura i trade che *esistono*; il tetto riduce quali
  trade nascono, non tocca la misura. ✅
- **(c) Il tetto cambia la popolazione dei trade, non la misura di V1.** L'`exit_R`
  vs controfattuale D è per-trade e resta pulito. ✅

**Nessun punto** in cui paniere/tetto sfiorano la logica o il logging di V1.

**Sfumatura onesta (non contaminazione, ma da sapere):** allargare il paniere
**cambia la popolazione** dei trade in due modi: (1) accelera la raccolta campioni
(più asset → più trade → il gate si riempie prima — è lo scopo), (2) introduce FX,
più mean-reverting, che in fascia 0.5-1.0R potrebbero comportarsi diversamente da
commodity/crypto. Questo rende il **campione del gate più eterogeneo**, ma NON
contamina la *misura* (ogni trade confronta V1 vs il suo D). È un effetto di mix,
non di metro. Lo segnalo perché, quando chiuderemo il gate a 10 campioni, sarà bene
guardare anche la composizione per classe, non solo la media.

---

## Guardrail (come per V1)

- **Tutto dietro flag, reversibile:**
  - Paniere: lista asset configurabile (proposta: `UNIVERSE` da env o flag
    `BASKET_FX_ENABLED` che appende i 3 FX all'universo). Rollback: rimuovi il flag.
  - Tetto B1: flag `CONCENTRATION_BLOCK_DUP` (anti-duplicato asset+direzione).
  - Tetto B2: `MAX_OPEN_POSITIONS` (già esistente) portato a 5-6.
  - Tetto B3: `MAX_OPEN_PER_DIRECTION` (default 0 = OFF).
  - Ogni flag indipendente, rollback in una riga nel `.env` VM.
- **Logging completo:** ogni blocco del tetto (setup + causa), costo/scan
  (`cost_report` esistente), asset attivi per scan (già in `scanner_runs.notes`).
- **Nessun trade aperto toccato retroattivamente:** paniere e tetto agiscono solo
  sulle *nuove* aperture; le posizioni vive non vengono toccate.
- **Vincolo "un esperimento alla volta" su V1:** non violato — paniere/tetto non
  toccano il trailing (B4). V1 resta l'unico esperimento sul *trailing*; questo è
  un esperimento sulla *selezione/apertura*, ortogonale.

---

## Cosa serve da voi prima del deploy

1. **Conferma esclusione USD/JPY** (sizing non currency-aware) e accettazione
   dell'errore conservativo ~15% su EUR/USD-GBP/USD-AUD/USD.
2. **Ri-misura spread in sessione** (lunedì a mercato aperto) per confermare
   GBP/USD nel blocco o tenerlo fuori.
3. **Valore del cap globale B2** (6 o 5) dopo aver guardato equity/margin budget.
4. Conferma del primo blocco a **3 FX (totale 8)**, staged.

Nessun deploy finché non leggiamo il doc insieme. Backlog post-V1 confermato:
**V2** (fascia 1.0-1.25R, evidenziata da #76) e **regola dinamica di correlazione**
(tesi-macro / cross-asset), entrambe pre-registrate, dopo la chiusura del gate V1.

## Limiti

Spread misurati a mercato chiuso (gonfiati) — ri-misurare in sessione. Coerenza
scoring cross-classe (A3) e comportamento mean-reverting FX (A4) sono ipotesi da
validare forward, non verificate. Errore di sizing FX ~15% accettato, non corretto
(il fix currency-aware è lavoro separato). USD/JPY escluso fino a quel fix.
