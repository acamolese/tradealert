# Sprint 6 — Piano operativo verso la scalata

Data: 2026-07-02. Stato: **IN ESECUZIONE** (gate pre-registrati, non modificati).

> **Stato di esecuzione al 2026-07-02:**
> - **A1 ESEGUITA** → verdetto **NEUTRO** (sostanza: il monitor NON distrugge valore, la mediana dice che ne aggiunge). Nessuna modifica a produzione. Esito in `docs/sprint6-monitor-replay.md`, script `jobs/monitor_close_replay.py`. Riapertura solo a n≥81 chiusure monitor.
> - **A2 ESEGUITA, round 2 il 2026-07-12 (n=45)** → verdetto **NON CONCLUSIVO** anche al round 2: bleed long dimezzato (-0.134R → -0.065R reale, -0.052R cf), residuo concentrato in Gold long (n=10, -0.244R). Nessuna modifica. Round 3 a **n≥60 long chiusi** (agenda riarmata). Esito in `docs/sprint6-long-gate.md`, script `jobs/long_gate_analysis.py`.
> - **A3 ESEGUITA il 2026-07-15 (17 trade)** → verdetto **NUOVI ASSET RESTANO** (exp_R nuovi -0.024 vs core -0.050, trigger rollback lontano; Nikkei mai aperto per margine minimo). Freeze sui futuri asset resta fino ad A4. Esito in `docs/sprint6-basket-gate.md`.
> - **A4.1/A4.2 IMPLEMENTATE**: cap in percentuale (bit-identici ai default), colonne `risk_at_open_eur`/`exit_r` con rilevamento runtime dello schema. **Passo manuale richiesto: applicare la migration `20260702150000_trades_risk_r.sql`** (supabase db push o SQL editor), poi eseguire `jobs/backfill_risk_r.py` sulla VM.
> - **A4.3**: gate di scaling documentato sotto; il check automatico di retrocessione (uncle point) è un **prerequisito da implementare prima di entrare nello step 1**, non serve allo step 0.
Destinatario: chi esegue le attività (dev/analista). Prerequisiti di accesso: repo locale con `.venv`, `.env` con chiavi Supabase e credenziali Capital, SSH alla VM (`ssh ubuntu@92.4.165.254`), account GitHub `acamolese` per il push.

## 0. Baseline al 2026-07-02 (da cui discendono le priorità)

Numeri calcolati dalla tabella `trades` su Supabase (86 trade chiusi con P&L, 18 aprile - 2 luglio):

| Metrica | Valore |
|---|---|
| P&L lordo totale | +€5.36 |
| Win rate | 30% (26/86) |
| Profit factor | 1.06 |
| Costi LLM stimati nel periodo | ~€30-38 (€0.35-0.50/giorno) |
| **Netto costi** | **negativo** |
| Top 5 trade (tutti short: Brent x2, Gold x2, Nasdaq) | +€44.54 |
| Somma degli altri 81 trade | -€39.18 |
| Long post fix bidirezionale (dal 2026-05-20) | n=30, -€15.39, WR 20% |
| Short post fix | n=34, +€20.96, WR 38% |
| Chiusure proposte dal monitor LLM ("Richiesta utente da monitor") | n=37, -€15.50 |
| Auto-close LLM monitor | n=15, +€3.53 |
| TP hit | n=7, +€52.86 |
| Stop hit | n=23, -€38.76 |
| Nuovi asset giugno (FX + Copper/HangSeng/Nikkei) | n=9 chiusi, ~-€5.9 |

Lettura strategica: il sistema è runner-dependent (tutto il profitto in 5 short arrivati a destinazione), il monitor LLM chiude il 60% dei trade ed è il bucket più negativo mai testato, il lato long non ha mai prodotto valore. Al capitale attuale i costi fissi LLM superano qualsiasi edge plausibile: la scalata passa dalla size, non da più asset o più segnali.

Riproduzione baseline: query `trades` filtrando `status='closed' and pnl is not null`; bucket per `exit_reason` (prefisso prima dei `:`), `direction`, mese di `closed_at`.

## 1. Regole di metodo (vincolanti)

1. **Gate pre-registrati.** Ogni analisi definisce metrica, soglia e azioni PRIMA di guardare i risultati. Questo documento è la pre-registrazione: non si modificano le soglie dopo aver visto i numeri.
2. **Un solo cambiamento a produzione alla volta.** Le analisi A1 e A2 sono read-only e possono girare subito. I cambi a produzione che ne derivano si applicano uno alla volta, annotando la data per segmentare i gate forward già aperti (V1/V2 trailing, open-rate check), che NON vanno toccati.
3. **Robustezza anti-outlier obbligatoria.** Lezione dei trade #42/#49: ogni verdetto va ricontrollato togliendo i 2 casi migliori e i 2 peggiori. Un verdetto che si rovescia togliendo 2 trade non è un verdetto.
4. **Nessun cap silenzioso.** Se un'analisi esclude trade (dati candele mancanti, trade pre-fix, ecc.) l'esclusione va contata e riportata nel documento di esito.
5. **Esiti negativi = risultato.** Si archivia con verdetto e non si ri-esplora senza dati nuovi (≥30 trade aggiuntivi). Vale già per: filtri entry (score, volatilità, chasing, regime), dedup 24h, uscita euro fissa, gap weekend.

## 2. Attività

Ordine di esecuzione: **A1 → A2** (A2 dipende dagli output di A1), **A3 e A4 in parallelo** (indipendenti).

---

### A1 — Replay controfattuale delle chiusure del monitor LLM

**Priorità: massima.** Il monitor chiude il 60% dei trade, è il bucket più negativo, e non è mai stato misurato contro un controfattuale. In un sistema runner-dependent un agente che propone CLOSE ogni 30 minuti è strutturalmente sospetto.

**Domanda:** se i trade chiusi dal monitor (proposte accettate + auto-close) fossero stati lasciati al solo bracket SL/TP + trailing, l'expectancy sarebbe stata migliore?

**Strumenti e template:** nuovo script `jobs/monitor_close_replay.py`, clonando l'impianto di `jobs/trailing_calibration.py` (ricostruzione traiettoria da candele Capital 5m, fallback 15m; short chiude all'ask, long al bid; se nella stessa candela vengono colpiti SL e TP si assume SL). Differenza chiave rispetto al template: la simulazione deve proseguire **oltre** il `closed_at` reale, perché il monitor ha troncato il trade.

**Passi operativi:**
1. Campione: tutti i trade chiusi con `exit_reason` che inizia per `manual:Richiesta utente da monitor` o `manual:auto-close LLM monitor` (oggi n≈52). Tenere i due sottogruppi distinti nel report.
2. Per ogni trade: ricostruire dalla apertura la traiettoria con la regola di trailing effettivamente in vigore alla data del trade (D pura, D+V1, D+V1+V2: usare le date di attivazione dei flag, vedi stato master 2026-06-09) e far decidere l'uscita solo a SL trailato / TP / orizzonte.
3. Orizzonte simulazione: fino a uscita bracket, con cap a **10 giorni di calendario** dopo `closed_at` reale; se all'orizzonte il trade è ancora aperto, mark-to-market sull'ultima candela (dichiararlo nel report, contare quanti casi sono).
4. I weekend restano dentro il replay (le candele del lunedì mostrano il gap): è voluto, il monitor potrebbe aver evitato gap reali e il controfattuale deve pagarli.
5. Normalizzare tutto in R (rischio = distanza entry-SL iniziale). Output per trade: `exit_r_reale`, `exit_r_controfattuale`, `delta_r`, direzione, asset, sottogruppo.
6. Esclusioni: trade senza candele disponibili (probabile per i più vecchi di aprile, la storia 5m di Capital è limitata) → contarli e riportarli. Se il campione utilizzabile scende sotto 25, fermarsi e rivalutare prima di emettere il verdetto.
7. Verifica di sanità: per 2-3 trade controllare a mano su Capital che la traiettoria ricostruita combaci con l'activity history.

**Gate pre-registrato (metrica primaria: `delta_r` medio = controfattuale − reale):**

| Verdetto | Condizione |
|---|---|
| **MONITOR DANNOSO** | delta_r medio ≥ +0.15R, E resta ≥ +0.10R togliendo i 2 migliori controfattuali, E delta_r mediano ≥ 0 |
| **MONITOR UTILE** | delta_r medio ≤ −0.15R, E resta ≤ −0.10R togliendo i 2 peggiori controfattuali |
| **NEUTRO** | tutto il resto |

**Azioni in funzione del risultato:**

- **DANNOSO** → disattivare le proposte CLOSE del monitor. Implementazione: nuovo flag `MONITOR_CLOSE_ENABLED` (default true) in `src/config.py`, letto in `src/position_monitor.py`: se false il monitor logga la valutazione LLM in `monitoring_events` (shadow, per controfattuale futuro) ma non invia proposte né auto-chiude. `AUTO_CLOSE_ENABLED` resta ma diventa irrilevante. Deploy con la regola aurea (commit → push → pull su VM → verifica), annotare la data per segmentare i gate V1/V2. Secondo step opzionale (solo dopo 2 settimane di conferma): ridurre la cadenza monitor da 30 a 60 minuti (taglia parte del 12% di costo LLM del monitor).
- **NEUTRO** → non toccare nulla. Archiviare il thread, riaprire solo con ≥30 nuovi trade chiusi dal monitor. L'auto-close resta per comodità operativa.
- **UTILE** → non toccare nulla, documentare, e considerare chiuso in positivo (il monitor sta salvando expectancy).
- In tutti i casi: produrre `docs/sprint6-monitor-replay.md` con numeri, esclusioni, breakdown per direzione e sottogruppo (serve ad A2).

**Effort stimato:** ~1 giornata (lo scheletro esiste già in `trailing_calibration.py`).

**Caveat dichiarati:** il controfattuale è una simulazione (over-optimism possibile, come già visto nello Sprint 4); l'assunzione SL-first è conservativa a favore del monitor; il cap a 10 giorni tronca eventuali runner lunghissimi (a sfavore del controfattuale). Entrambi i bias remano CONTRO il verdetto "dannoso", quindi un verdetto dannoso è credibile; un verdetto "utile" marginale va letto con più cautela.

---

### A2 — Gate direzionale sul lato long

**Priorità: alta, ma DOPO A1.** Il long perde (-€15.39, WR 20% post fix) ma non sappiamo se è un problema di selezione o un artefatto della gestione: se il monitor ha tagliato sistematicamente i long, il bleed sparisce nel controfattuale di A1. Decidere prima di A1 significherebbe rischiare di spegnere il lato sbagliato del sistema.

Nota: il kill switch direzionale dello Sprint 2 misurava se il sistema *genera* short (gate passato: li genera). Questo è un gate nuovo, sull'*expectancy realizzata* del lato long.

**Passi operativi:**
1. Campione: trade chiusi dal 2026-05-20 (fix bidirezionale live), separati long/short, normalizzati in R.
2. Incrociare con l'output di A1: per i long chiusi dal monitor usare ANCHE `exit_r_controfattuale`.
3. Robustezza: (a) togliere i 2 long peggiori; (b) solo 5 asset core (escludere FX e trending, che inquinano giugno); (c) breakdown per asset per verificare che il bleed non sia un solo asset.

**Gate pre-registrato:**

| Verdetto | Condizione |
|---|---|
| **LONG DA BLOCCARE** | expectancy_R long ≤ −0.10R sia sui dati reali SIA nel controfattuale A1 (bracket-only), su n≥30, robusto ad (a) e (b) |
| **PROBLEMA DI GESTIONE, NON DI SELEZIONE** | expectancy_R long reale ≤ −0.10R MA controfattuale ≥ 0 |
| **NON CONCLUSIVO** | tutto il resto |

**Azioni in funzione del risultato:**

- **LONG DA BLOCCARE** → non spegnere del tutto (lezione Sprint 2: è il mercato a decidere la distribuzione; un blocco totale è irreversibile come informazione). Implementare **rischio dimezzato sul lato long**: flag `LONG_RISK_FACTOR` (default 1.0, deploy a 0.5) applicato in `src/risk.py::calculate_size` quando `direction='long'`. Il lato long continua a generare dati a metà costo. Gate di revisione: dopo 20 nuovi long chiusi, se expectancy_R risale > 0 riportare a 1.0; se resta ≤ −0.10R valutare blocco pieno.
- **PROBLEMA DI GESTIONE** → nessun intervento sul lato long: la fix è l'esito di A1 (monitor). Verificare dopo 20 nuovi long chiusi post-cambio-monitor che il bleed sia rientrato.
- **NON CONCLUSIVO** → nessuna azione, ripetere l'analisi a +15 long chiusi (data-trigger, non tempo-trigger).
- In tutti i casi: `docs/sprint6-long-gate.md` con i numeri.

**Effort stimato:** mezza giornata (riusa gli output A1).

---

### A3 — Congelamento paniere e gate sui nuovi asset

**Priorità: media. Zero sviluppo, è una decisione di processo con scadenza.**

**Regola immediata (da oggi):** FREEZE del paniere. Nessun nuovo asset finché A1/A2 non sono chiuse e il gate qui sotto non è scaduto. Scalare aggiungendo asset diluisce il sample e moltiplica i costi di scan; scalare la size no.

**Gate pre-registrato sui 6 asset aggiunti a giugno** (EUR/USD, AUD/USD, GBP/USD, Copper, Hang Seng, Nikkei — oggi: 9 chiusi, ~-€5.9):

- Finestra: fino a **15 trade chiusi complessivi** sui 6 nuovi asset, oppure **2026-08-15**, quello che arriva prima.
- Metrica: expectancy_R dei nuovi asset vs expectancy_R dei 5 core nello stesso periodo.
- **Se expectancy_R(nuovi) < expectancy_R(core) − 0.10R** → rollback: `BASKET_FX_ENABLED=false` e `BASKET_TREND_ENABLED=false` nel `.env` della VM (nessun redeploy, backup già in `.env.bak.sprint5`). Archiviare con verdetto.
- **Se expectancy_R(nuovi) ≥ expectancy_R(core) − 0.10R** → i nuovi asset restano, il freeze sui *futuri* asset resta comunque fino al primo step di scaling (A4).
- Caso limite: se al 2026-08-15 i nuovi asset hanno prodotto <8 trade chiusi, il verdetto è automatico: **rollback** (un asset che non produce setup non paga il costo di scan che genera, stesso criterio già usato per gli FX nella validazione forward).

**Effort:** 15 minuti oggi (annotare la scadenza), mezza giornata al trigger per l'analisi.

---

### A4 — Infrastruttura di scaling: R persistiti, cap in percentuale, scala del capitale

**Priorità: media, parallelizzabile. È lavoro dev, non tocca la logica di trading.**

**A4.1 Persistere il rischio e l'R di uscita su ogni trade.**
Oggi ogni analisi ricostruisce gli R a posteriori dalle candele. Aggiungere:
- Migration Supabase: colonne `trades.risk_at_open_eur numeric` e `trades.exit_r numeric`.
- `src/executor.py`: al momento dell'apertura scrivere `risk_at_open_eur` (size × distanza entry-SL, convertita in EUR con il tasso già usato dal fix currency-aware).
- Percorso di chiusura (`src/reconcile.py` e chiusure monitor): calcolare `exit_r = pnl / risk_at_open_eur` alla chiusura.
- Backfill best-effort sui trade storici dove i dati lo permettono (riportare quanti restano NULL).

**A4.2 Cap di rischio in percentuale del capitale.**
Oggi i cap sono in EUR assoluti (`MAX_LOSS_PER_TRADE_EUR=5`, `WEEKLY_DRAWDOWN_CAP_EUR=20`): ogni step di scala richiederebbe di ritoccare costanti a mano. Introdurre in `src/config.py`:
- `ACCOUNT_RISK_CAPITAL_EUR` (oggi 100),
- `MAX_LOSS_PER_TRADE_PCT` (oggi 5%), `WEEKLY_DRAWDOWN_CAP_PCT` (oggi 20%),
- i valori EUR diventano derivati; le env EUR esistenti restano come override legacy con warning nel log.
Comportamento bit-identico ai valori attuali al primo deploy (verificarlo nei log: stessi cap calcolati).

**A4.3 Scala del capitale con gate pre-registrato (LA definizione di "il sistema funziona").**

| Step | Capitale | Rischio/trade | Cap settimanale | Condizione di ingresso |
|---|---|---|---|---|
| 0 (oggi) | €100 | €5 | €20 | — |
| 1 | €300 | €15 | €60 | vedi sotto |
| 2 | €1000 | €50 | €200 | stessa condizione, ricalcolata sui trade dello step 1 |

**Condizione di ingresso allo step successivo** (tutte e tre, su una finestra di **≥50 trade chiusi** iniziata DOPO che le decisioni A1/A2 sono in produzione, così il sample misura il sistema nella sua configurazione finale):
1. expectancy ≥ **+0.15R** per trade;
2. profit factor ≥ **1.3**;
3. P&L del periodo **al netto dei costi LLM** > 0 (i costi si leggono dal `cost_report` settimanale).

**Uncle point per ogni step (condizione di retrocessione):** drawdown dal picco > **30% del capitale dello step** → si torna allo step precedente e si riapre la diagnosi. La retrocessione è automatica e non negoziabile: va implementata come check nello scanner (stesso pattern del weekly drawdown cap), non lasciata alla disciplina.

Nota importante: al passaggio di step cambia SOLO la size. Universo, soglie, prompt, trailing restano congelati. Se si vuole cambiare altro, si cambia prima, si ricomincia la finestra dei 50 trade, poi si scala.

**Effort stimato:** ~1 giornata complessiva (A4.1 + A4.2), il gate A4.3 è documentazione + un check nello scanner.

---

## 3. Sequenza e dipendenze

```
Settimana 1:  A1 (script + replay + verdetto)          A4.1/A4.2 in parallelo
Settimana 2:  A2 (usa output A1) → eventuale deploy della PRIMA modifica (una sola)
              A3: freeze attivo da subito, scadenza gate segnata (15 trade nuovi asset o 2026-08-15)
Poi:          eventuale SECONDA modifica (se A1 e A2 producono entrambe un'azione, in sequenza non insieme)
              Da quando l'ultima modifica è live: parte la finestra ≥50 trade del gate di scaling A4.3
Continuo:     gate già aperti (V1/V2 trailing, open_rate_check) proseguono invariati
```

Se A1 e A2 risultano entrambe NEUTRO/NON CONCLUSIVO: nessuna modifica a produzione, la finestra dei 50 trade per lo scaling parte comunque (il sistema attuale diventa il candidato da validare così com'è).

## 4. Matrice decisionale riassuntiva

| Analisi | Esito | Azione | Rollback |
|---|---|---|---|
| A1 monitor | DANNOSO | `MONITOR_CLOSE_ENABLED=false` (monitor in shadow) | flag a true |
| A1 monitor | NEUTRO / UTILE | nessuna, archivia | — |
| A2 long | DA BLOCCARE | `LONG_RISK_FACTOR=0.5`, revisione a +20 long | flag a 1.0 |
| A2 long | PROBLEMA GESTIONE | nessuna (fix = esito A1), verifica a +20 long | — |
| A2 long | NON CONCLUSIVO | ripeti a +15 long chiusi | — |
| A3 nuovi asset | peggio del core −0.10R, o <8 trade al 2026-08-15 | `BASKET_FX_ENABLED=false`, `BASKET_TREND_ENABLED=false` | flag a true |
| A3 nuovi asset | in linea col core | restano; freeze futuri asset fino a step 1 | — |
| A4.3 scaling | 50 trade: exp ≥ +0.15R, PF ≥ 1.3, netto costi > 0 | step di capitale successivo | uncle point −30% → step precedente |
| A4.3 scaling | condizioni non raggiunte a 50 trade | NO scaling; nuova diagnosi sui dati della finestra | — |

## 5. Cosa NON fare (già deciso, non ri-esplorare senza ≥30 trade nuovi)

- Nuovi filtri di SELEZIONE entry: score-based, volatilità/regime, chasing/estensione, dedup 24h — quattro verdetti negativi pre-registrati (Sprint 4-5). La leva che funziona è la gestione.
- Uscita a euro fissi (+1/-1): negativa, taglia i runner (e i runner sono tutto il P&L).
- Aggiungere asset o alzare `MAX_OPEN_POSITIONS`: giugno ha dimostrato che più volume senza edge peggiora il netto.
- Toccare le soglie del trailing D/V1/V2 mentre i gate forward sono aperti.
- Scalare il capitale "a sensazione" prima del gate A4.3.
