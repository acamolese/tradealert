# Sprint 7 Fase B — Entrata deterministica, LLM solo sul monitor

Data switch: 2026-07-14. Stato: LIVE (flag `SCORING_LLM_OFF=true` sul .env VM).
Rollback: `SCORING_LLM_OFF=false` (una riga, nessun redeploy).

## Perché (decisione a valle della Fase A, chiusa 2026-07-14)

- Score LLM in entrata: correlazione ≈ 0 con gli esiti (giugno, n=83).
- Fase A: concordanza asset+direzione 25% su 20 confronti (stream diversi).
- Replay controfattuale vincolante: LLM -0.391R vs deterministico -0.381R
  = pareggio non robusto. Nessuno apre meglio.
- Costi: l'entrata LLM pesa l'83% della bolletta API (~$5/mese su $6.05).
  A parità di performance vince chi costa zero.

## Cosa cambia (e cosa NO)

- Lo scan NON chiama più l'LLM: le proposte vengono da
  `src/entry_shadow.py::deterministic_proposals` (regola v1-momentum di
  Fase A, INVARIATA: tradeable, spread ≤ 0.5%, |dpc| ≥ 0.5%, direzione =
  segno del momentum, stop = max(1.5×ATR%, 0.5%), target = 2×stop).
- **Estensione dichiarata** rispetto a Fase A: non solo il pick n.1 ma la
  lista ordinata per |dpc| decrescente, così dedup 24h, budget e cap fanno
  scendere ai successivi come con le proposte LLM. Il n.1 resta identico
  al pick di Fase A.
- Score sintetico 7.0 + min(|dpc|/10, 0.9): preserva il ranking, sta nella
  banda LLM tipica (7.0–7.5) e resta azzerabile dalla penalità -2 dei
  guardrail macro. Thesis marcata `[v1-momentum]` (usata dal gate).
- INVARIATI: pre-filtro, guardrail macro deterministici, dedup 24h,
  MIN_SCORE_THRESHOLD, cap concentrazione B1/B3, budget, sizing, conferma
  Telegram, trailing D+V1+V2, **monitor LLM** (A1: non dannoso, mediana a
  favore), avviso -0.5R, agenda.
- Shadow di Fase A spento in Fase B (sarebbe un duplicato del flusso live);
  lo shadow scoring Sprint 4 è escluso esplicitamente quando il flag è on.

## Gate forward PRE-REGISTRATO (prima di vedere qualsiasi esito)

Trigger automatico (agenda 08:20): **20 trade chiusi** nati da signal con
thesis `[v1-momentum]` e chiusi dal 2026-07-14.

Metrica: expectancy_R (media `trades.exit_r`) dei 20 deterministici vs i
20 trade LLM immediatamente precedenti lo switch.

| Esito | Condizione | Azione |
|---|---|---|
| PROSEGUI | exp_det ≥ exp_llm − 0.05R | Fase B resta; si valuta Fase C |
| ZONA GRIGIA | −0.15R < delta < −0.05R | proseguire fino a 40 trade |
| ROLLBACK | exp_det ≤ exp_llm − 0.15R | `SCORING_LLM_OFF=false` |

Da monitorare a mano nel frattempo (non gate): tasso di apertura (baseline
storica ~1.47 signal/giorno) e bolletta API (attesa ~$1/mese, solo monitor
+ events). Nota onesta: 20 trade sono pochi; il gate protegge dal disastro,
non certifica l'equivalenza. La certificazione arriva col tempo e con la
finestra A4 (50 trade) che continua a correre indipendente.

## Contesto Fase C

Scala del capitale (`ACCOUNT_RISK_CAPITAL_EUR` 100→300) SOLO dopo gate A4
(exp ≥ +0.15R, PF ≥ 1.3 su 50 trade) E check automatico uncle-point -30%
(prerequisito A4.3, da implementare). La Fase B abbassa il breakeven ma
non crea edge: quello va cercato dove i dati lo indicano (gestione uscite).
