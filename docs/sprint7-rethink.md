# Sprint 7 (proposta) — Ripensare TradeAlert per la profittabilità

Data: 2026-07-02. Stato: STUDIO STRATEGICO, nessuna decisione presa.
Origine: obiezione dell'utente allo Sprint 6 ("non hai studiato davvero come
renderla più profittevole, anche rivedendo tutto"). L'obiezione è fondata:
lo Sprint 6 ottimizza il processo di validazione ATTORNO al motore attuale,
ma non mette in discussione il motore. Questo documento lo fa.

## 1. Diagnosi strutturale: tre vincoli che il tuning non può rimuovere

### V1 — Il selettore non seleziona

Verificato oggi sul campione pieno (83 trade con score e exit_R):

- correlazione score LLM vs exit_R: **-0.022** (zero);
- 95% dei trade vive in score 7.0-7.5 (57 a 7.0 exp +0.065R, 22 a 7.5 exp +0.114R,
  i 3 trade a 8.0 hanno exp -0.35R);
- coerente con Sim C (Sprint 4): lo score pulito non separa WIN/LOSS.

Il cuore del sistema, un LLM che assegna 0-10 a feature tecniche pubbliche su
candele 4H, non ha potere predittivo misurabile. Non è un difetto di prompt o
di temperatura (già sistemati): un LLM non ha alcun vantaggio informativo su
RSI/ATR/slope che il mercato non abbia già prezzato. Produce un gate quasi
casuale più una narrativa convincente. I 5 esperimenti di selezione falliti
(score, volatilità, chasing, regime, dedup) sono 5 conferme indipendenti.

### V2 — Il sistema non è backtestabile, quindi non può convergere

Il motore è stocastico (LLM) e valutabile solo forward: ~35 trade/mese.
Ogni ipotesi costa 1-2 mesi di attesa e il campione non basta mai (86 trade in
2.5 mesi, e il sample 38-65 è già dichiarato "esaurito"). A questo ritmo di
iterazione la probabilità di trovare e VALIDARE un edge prima di esaurire
pazienza/capitale è vicina a zero. Questo è il vincolo più grave: non è un
problema di strategia ma di **velocità di apprendimento**.

### V3 — L'economia non torna a questa scala, e un costo è ignoto

- Costi fissi LLM ~€10-15/mese contro capitale di rischio €100: 10-15%/mese di
  attrito. Nessun edge realistico (0.1-0.2R/trade) lo copre a questa size.
- 23 trade su 83 durano ≥24h e pagano overnight financing sui CFD: **mai
  misurato**, non è scorporato nel pnl. Su swing multi-day può valere
  0.01-0.05%/notte. Da quantificare prima di qualsiasi scaling.
- Dove sono nati i profitti: 5 short runner su commodities in trend
  (+€44.54) più la gestione delle uscite. Questo è trend-following classico,
  una famiglia di strategie nota, studiata e soprattutto **backtestabile**.

## 2. Le opzioni sul tavolo

### Opzione A — Inversione architetturale: motore deterministico, LLM declassato (RACCOMANDATA)

Ribaltare i ruoli. Oggi: LLM decide, Python fa i guardrail. Domani: **Python
decide con regole esplicite backtestabili, l'LLM (Haiku, quasi gratis) fa solo
risk-veto su news/eventi e la narrativa Telegram**.

Perché è l'unica opzione che rimuove i tre vincoli insieme:
- V1: una regola esplicita (es. breakout Donchian + filtro trend ATR) è almeno
  onesta: se non ha edge, il backtest lo dice subito;
- V2: si testa su 5-10 anni di candele × 20-30 asset = migliaia di trade
  simulati in minuti, invece di 35 al mese dal vivo;
- V3: costo LLM crolla (scanner deterministico, Haiku solo su eventi), e
  spread + overnight entrano nel simulatore come costi espliciti.

Nulla del lavoro fatto si butta: infrastruttura (VM, cron, Telegram, Supabase,
executor, reconcile), trailing calibrato (D/V1/V2, l'unica leva validata),
risk caps, e i 93 trade reali che diventano il set di validazione del
simulatore.

**Roadmap con gate pre-registrati:**

| Fase | Contenuto | Gate per passare oltre |
|---|---|---|
| M1 (2-3 gg) | Fetch storico candele (Capital API + fonte esterna di riserva) per i 5 core + 15-25 asset liquidi, 5-10 anni dove disponibili | copertura dati ≥5 anni sui core |
| M2 (3-5 gg) | Simulatore: bracket SL/TP + trailing D/V1/V2 + spread reali + overnight financing + slippage. Riuso della logica di `monitor_close_replay.py` | — |
| M3 (1-2 gg) | **Validazione contro la realtà**: replay dei 93 trade reali nel simulatore; deve riprodurre gli exit_R osservati (scarto aggregato ≤0.1R medio) | se non li riproduce, il simulatore è rotto: STOP e fix |
| M4 (1 sett) | Test famiglie note: Donchian breakout, MA cross + filtro ATR, momentum time-series, long e short, multi-timeframe. Walk-forward: parametri scelti in-sample, giudizio SOLO out-of-sample | candidata: exp ≥ +0.20R OOS **al netto dei costi**, robusta su sottoperiodi e senza dipendere da 2 trade (test anti-#42/#49) |
| M5 (1 mese) | Shadow live della candidata accanto al sistema attuale (stessa infrastruttura, logging-only) | shadow coerente col backtest → switch del motore dietro flag |

Effort totale: ~2-3 settimane di lavoro + 1 mese di shadow. Rischio principale:
scoprire che NESSUNA famiglia semplice batte i costi su questi asset/orizzonti.
Ma quella sarebbe una risposta vera, ottenuta in settimane invece che in anni
di forward test, e a quel punto l'opzione onesta è la D.

### Opzione B — Tenere l'LLM ma dargli dati con edge potenziale

News flow real-time, posizionamento COT, term structure dei future, flussi.
Contro: costi dati, complessità, e resta non-backtestabile (V2 intatto).
L'LLM avrebbe senso su dati NON strutturati (news) come **filtro di rischio**,
non come selettore. Bocciata come piano principale; il risk-veto news
sopravvive dentro l'opzione A.

### Opzione C — Status quo: validare il sistema attuale con la finestra dei 50 trade

È il piano Sprint 6 già attivo. Onestà sulla probabilità: con PF storico 1.06
ed expectancy +0.05R, la probabilità che 50 trade mostrino exp ≥ +0.15R e
PF ≥ 1.3 è bassa (serve che il regime resti favorevole agli short runner).
Va comunque portata a termine: costa zero (l'agenda avvisa da sola) e fornisce
il benchmark contro cui giudicare la candidata dell'opzione A.

### Opzione D — Spegnere o ridurre a paper trading

Legittima se: M4 non produce candidate sopra i costi, E la finestra dei 50
trade fallisce il gate. A quel punto il valore del progetto è
l'infrastruttura e il metodo, non il P&L, e tenerlo acceso a €0.35-0.5/giorno
sarebbe una tassa sull'affetto per il progetto.

## 3. Raccomandazione operativa

**A e C in parallelo.** C è già attiva e automatica (agenda Sprint 6). A parte
subito con M1-M3, perché non tocca produzione (tutto offline) e non viola la
regola "un esperimento alla volta" (che riguarda i cambi al sistema live).

Decisione finale tra ~2 mesi con due evidenze indipendenti sul tavolo:
1. il sistema attuale ha passato il gate dei 50 trade? (C)
2. esiste una candidata deterministica che batte i costi out-of-sample? (A/M4)

| C passa | A trova candidata | Decisione |
|---|---|---|
| sì | sì | confronto diretto: si scala quella con exp OOS/forward migliore |
| sì | no | si scala il sistema attuale (step 1, con uncle point) |
| no | sì | switch del motore (M5 shadow → live), capitale resta 100 |
| no | no | opzione D: paper trading o spegnimento |

## 4. Cosa NON è questo documento

Non è una promessa che il trend-following deterministico funzioni. È lo
spostamento della domanda "il sistema è profittevole?" dal terreno dove non
può avere risposta (35 trade/mese, selettore casuale) al terreno dove la
risposta arriva in settimane (backtest onesto con costi reali + shadow).
