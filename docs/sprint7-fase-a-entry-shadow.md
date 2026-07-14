# Sprint 7 Fase A — Shadow del selettore d'entrata deterministico

Data attivazione: 2026-07-02 sera. Stato: LIVE (logging-only).
Obiettivo: decidere se l'LLM può uscire dall'ENTRATA (dove lo score ha
correlazione -0.02 con gli esiti ma costa l'87% della bolletta API),
restando solo sul monitor (dove A1 ne ha misurato il valore).

## Cosa gira

A ogni scan, dopo il pre-filtro deterministico, `src/entry_shadow.py` logga
in `monitoring_events` (event_type=`entry_shadow`) cosa un picker SENZA LLM
avrebbe aperto. Lo scan reale prosegue invariato: zero impatto su trade.
Rollback: `ENTRY_SHADOW=false` nel `.env` VM (default attivo).

## Regola v1-momentum (FISSA per tutta la raccolta)

Tra i candidati del pre-filtro, tradeable, spread ≤ 0.5% e |variazione
giornaliera| ≥ 0.5%: si prende il mover più forte (max |daily_pct_change|),
direzione = segno del momentum, stop = max(1.5×ATR%, 0.5%), target = 2×stop.
Nessun ritocco alla regola durante la Fase A.

## Fine della Fase A (automatica, avviso Telegram dall'agenda giornaliera)

Trigger: **20 signal LLM confrontabili** (evento shadow nello stesso scan,
finestra ±20 min) oppure **2026-07-31**, quello che arriva prima.
`jobs/sprint6_agenda.py` calcola e notifica concordanza su asset e su
asset+direzione.

## Guida decisionale PRE-REGISTRATA (concordanza asset+direzione)

| Concordanza | Decisione |
|---|---|
| ≥ 60% | Fase B: switch al selettore deterministico dietro flag, LLM resta solo sul monitor. Costi API da ~€13 a ~€2/mese. |
| 30-60% | Fase B possibile ma con gate forward stretto: open-rate e expectancy dei primi 20 trade confrontati col baseline. |
| < 30% | Stream troppo diversi: analizzare le divergenze (chi apre meglio?) prima dello switch. |

Nota di onestà: dato che lo score LLM non predice gli esiti, una concordanza
bassa NON significa che il deterministico sia peggiore; significa che lo
switch cambia la distribuzione dei trade e va validato forward, non assunto
equivalente.

## Replay controfattuale a fine fase (PRE-REGISTRATO 2026-07-10)

Oltre alla concordanza, alla chiusura della fase si esegue
`jobs/entry_shadow_replay.py`: confronto PAIRED dei due stream sugli scan
in cui esistono entrambi (signal LLM + pick shadow, ±20 min), stessa
simulazione validata M3 (entry = open prima candela HOUR post-scan,
bracket TP + trailing live, SL-first, orizzonte 15gg). Ogni stream usa i
propri stop/target. Criterio, deciso PRIMA di guardare l'aggregato:

- n minimo 15 pair validi, sotto è solo descrittivo;
- un vincitore c'è solo se |delta expectancy| ≥ 0.15R E il delta senza i
  2 pair più favorevoli resta ≥ 0.10R nello stesso segno (anti-outlier);
- altrimenti NON CONCLUSIVO: decide la sola guida di concordanza sopra.

Il replay risponde a "chi apre meglio?", complementare alla concordanza
("quanto spesso scelgono la stessa cosa?"). Verdetto aggregato da NON
calcolare prima dell'avviso di fine fase; consentito il replay di singoli
casi con `--only` (es. divergenza Brent 2026-07-07, richiesta utente).

> **Deviazione dichiarata (2026-07-12):** su richiesta esplicita dell'utente
> ("i dati dicono che potremmo fare lo switch?") il replay aggregato è stato
> eseguito a 18/20 confronti (17 pair simulati, 1 scartato, n≥15 ok).
> Esito PROVVISORIO: LLM -0.114R vs shadow -0.199R, delta -0.085R che si
> ROVESCIA a +0.137R senza i 2 pair migliori dell'LLM → NON CONCLUSIVO,
> nessuno dei due apre meglio in modo robusto. Caveat: i 5 pair del 10/07
> sono troncati a ~1.5 giorni di candele (weekend). I criteri NON vengono
> modificati; il verdetto vincolante resta quello a fase chiusa (20 confronti
> o 2026-07-31), stesso script, stesse soglie.

## ESITO FINALE (2026-07-14, fase chiusa a 20 confronti)

- **Concordanza asset+direzione: 25%** (5/20 asset) → fascia bassa: i due
  selettori aprono stream diversi.
- **Replay controfattuale vincolante** (18 pair simulati, 2 scartati per
  candele mancanti): LLM -0.391R vs shadow -0.381R, delta +0.010R (senza
  top2 -0.107R) → **NON CONCLUSIVO: pareggio pieno**. Finestra difficile
  (l'escalation USA-Iran del weekend ha stoppato a -1R quasi tutti i pick
  di entrambi, 10/18 pair).
- Lettura combinata con corr score/esiti ≈ 0 (giugno): l'LLM in entrata non
  apre meglio di una regola momentum gratuita. Lo switch (Fase B) non è
  sostenuto dalla performance ma dai costi (83% bolletta API); da fare
  dietro flag con gate forward sui primi 20 trade, come da guida 30-60%
  (la fascia <30% richiedeva l'analisi divergenze: è questo replay).

## Contesto (Fasi B e C del piano "aggressivo ma razionale")

- **Fase B**: flag `SCORING_LLM_OFF` (da costruire): il pick deterministico
  diventa il signal, stessa pipeline a valle (guardrail, conferma, sizing,
  trailing, monitor LLM invariato).
- **Fase C**: scala del rischio via `ACCOUNT_RISK_CAPITAL_EUR` 100→300,
  SOLO dopo l'implementazione del check automatico di retrocessione
  (uncle point -30%, prerequisito A4.3) e a Fase B assestata.
- Bocciati oggi con test pre-registrati: no-TP/time-stop
  (`docs/sprint7-convexity-test.md`), motore deterministico swing su CFD
  (M4), cambio orizzonte mensile su CFD (financing) — quest'ultimo vive
  già fuori TradeAlert col broker ETF dell'utente.
