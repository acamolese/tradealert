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
