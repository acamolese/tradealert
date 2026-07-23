# Sprint 8 — Ri-validazione calibrazione trailing su storico lungo (pre-registrato)

Data: 2026-07-23. Stato: **PRE-REGISTRATO prima di calcolare gli esiti.**
Soglie e predizione fissate ORA (anti-HARKing).

## Origine

Il test calibrazione trailing di giugno (`docs/sprint4-trailing-calibration.md`,
`trailing_calibration_result`) trovo' che un trail piu' stretto nella fascia
0.5-1.0R batte l'opzione D. Fu il PRIMO risultato non-negativo del progetto (leva
di GESTIONE, non selezione). Sintesi deployata: **V1** (rampa di lock solo in
0.5-1.0R, D puro sopra), LIVE dietro flag `TRAIL_V1_LOWBAND` con gate forward
aperto. Restava "provvisorio": **sample di 27 trade** (giugno, choppy),
regime-dipendente, over-optimism sim 5m.

Ora esiste un harness robusto (backtest 2020-2026, candle orarie bid/ask, motore
live, gia' usato per i test frequenza): si puo' ri-validare la calibrazione su
**migliaia** di trade invece di 27, quasi tutti out-of-sample rispetto alla scelta
di giugno. Questo e' cio' che rende sensato riprendere il test ORA.

## Metodo

Stessi ENTRY (v1-momentum, ~6300 trade 2020-2026) per tutte le varianti: si isola
SOLO l'effetto della regola di trailing. Per ogni entry si simula l'uscita col
motore live (SL k·ATR, TP rr·SL, spread bid/ask, overnight fee) sotto 5 offset:

- **D** — opzione D pura (granulare 0.25 + lock TP-aware), `make_offset_fn(F,F)`.
- **V1** — D + rampa fascia bassa 0.5-1.0R (0.5→−0.25, 0.75→−0.10, 1.0→BE),
  `make_offset_fn(T,F)`.
- **V1+V2 (live)** — V1 + rampa 1.0-1.25R, `make_offset_fn(T,T)`. Sistema attuale.
- **gap0.25** — trail stretto su tutta la corsa: `max(−1, peak_r − 0.25)` (il
  "vincitore provvisorio" di giugno, senza lock TP, come nel test originale).
- **gap0.50** — contesto.

Metrica: expectancy_R netta. Classificazione dei trade (peak_r raggiunto sotto D,
fisso per tutte le varianti): **fascia bassa** (peak 0.5-1.0R, no full TP: dove
V1/gap agiscono), **trend** (peak ≥ 1.25R: dove V1 deve = D, gap taglia).
Breakdown per periodo (IS 2020-2023 / OOS 2024-2026) e per asset.

## Predizione PRE-REGISTRATA (prima dei numeri)

Il sample di giugno era choppy e piccolo; lo storico 2020-2026 contiene grandi
TREND (BTC, oro, indici). Ipotesi: **gap0.25 NON reggera' sul lungo** perche'
taglia i trend che corrono (il suo punto debole gia' notato: #42 Brent 2.0R→0.78R
stretto), mentre **V1 reggera'** perche' per costruzione non tocca peak≥1.25R
(delta-trend ~0) e recupera solo la fascia bassa. Se cosi', il campione lungo
CONFERMA la scelta V1 su gap0.25. Se invece gap0.25 vince anche qui, e' piu'
robusto del previsto e va riconsiderato.

## Gate PRE-REGISTRATO

Sia `exp(X)` l'expectancy_R netta della variante X sugli stessi entry.

| Verdetto | Condizione |
|---|---|
| **V1 CONFERMATA** | `exp(V1) − exp(D) ≥ +0.03R` **e** robusto (senza i 2 migliori trade ≥ +0.01R) **e** positivo in IS E OOS **e** delta-trend `exp(V1|trend) − exp(D|trend) ≥ −0.02R` (non taglia i trend). |
| **gap0.25 SUPERIORE** | `exp(gap0.25) − exp(V1) ≥ +0.05R` robusto e in IS E OOS. Riapre gap0.25 come candidata (pre-registrando il deploy). |
| **NULLA BATTE D** | nessuna variante supera D di +0.03R robusto → la calibrazione fascia-bassa non regge sul lungo; rivalutare se tenere V1 live. |

Nota: soglie piccole (+0.03R) perche' la leva agisce solo sulla frazione di trade
in fascia bassa; diluito su tutti i trade il segnale e' piccolo per costruzione.
Il breakdown fascia-bassa mostra l'effetto non diluito (atteso piu' grande).

Sola lettura. Uso: `PYTHONPATH=$PWD .venv/bin/python jobs/trailing_variant_backtest.py`

## ESEGUITO 2026-07-23 — il vantaggio dei gap stretti e' ARTEFATTO di granularita'

Script `jobs/trailing_variant_backtest.py`, 6274 entry, 2020-2026.

| variante | exp_R | vs D | fascia bassa vs D | trend vs D |
|---|---|---|---|---|
| D | -0.1042 | +0.0000 | +0.000 | +0.000 |
| V1 | -0.1035 | +0.0008 | +0.262 | **-0.144** |
| V1+V2 (live) | -0.1031 | +0.0012 | +0.262 | -0.168 |
| gap0.50 | -0.0761 | +0.028 | +0.587 | -0.349 |
| gap0.25 | -0.0300 | +0.074 | +0.815 | -0.360 |
| gap0.15 | +0.0002 | +0.104 | — | — |
| **gap0.10** | **+0.0204** | **+0.125** | — | — |

**Il gate pre-registrato sparava "gap0.25 SUPERIORE".** Ma il check diagnostico
(gap piu' stretti) lo falsifica: exp_R **monotòna col restringersi dello stop**,
fino a gap0.10 (+0.02R, il migliore). Uno stop a 0.10R sopra il picco e'
irrealizzabile (colpito dal bid-ask bounce e dal rumore tick all'istante): che la
sim ORARIA lo premi come ottimo dimostra che non cattura il rumore che punisce gli
stop stretti. "Piu' stretto = sempre meglio, senza ottimo interno" e' la firma
dell'over-optimism da granularita' — esattamente il caveat #1 pre-registrato a
giugno, qui amplificato dalle candele orarie (piu' grosse delle 5m di allora).
**Il vantaggio di gap0.25 e' in gran parte, forse tutto, artefatto. NON deployabile.**

**Su V1 (la variante LIVE) la predizione era sbagliata due volte:**
1. V1 su 6274 trade e' ≈ D (+0.0008R): il vantaggio visto a giugno su 27 trade NON
   si conferma su larga scala.
2. V1 TAGLIA i trend (-0.144R sui 1668 peak≥1.25R), non "delta-trend zero" come
   sul sample 27. Meccanismo emerso solo su larga scala: un trade che diventera'
   trend forte passa prima per la fascia 0.5-1.0R; li' lo stop piu' stretto di V1
   lo puo' stoppare su un ritraccio temporaneo PRIMA che il trend parta. V1 sposta
   valore dalla fascia bassa (+0.262R) ai trend (-0.144R), somma ≈ 0.

## Verdetto reale

- **Nessuna variante batte D in modo AFFIDABILE.** V1 ≈ D (vantaggio giugno non
  confermato, e taglia trend nascenti che il sample piccolo nascondeva). I gap
  stretti "vincono" solo per artefatto di granularita'.
- Il backtest ORARIO e' strutturalmente inadatto a giudicare stop stretti: li
  sovrastima. La domanda "un trail piu' stretto ha edge reale?" richiede dati
  TICK/fini che catturino il rumore — non risolvibile con candle orarie/5m.

## Azione

- **NON deployare gap0.25** (artefatto).
- **V1 live**: non fa danno (≈D) ma non aggiunge valore su larga scala e taglia
  qualche trend nascente. Decisione utente: tenerlo (innocuo, ottimizzato per
  campioni choppy) o tornare a D (`TRAIL_V1_LOWBAND=false`, piu' semplice, non
  taglia i trend). Nessuna urgenza: l'impatto netto e' ~0.
- **Fase 2 (se si vuole chiudere la domanda):** validare gap-stretto vs D su
  candle TICK/fini su periodo recente, dove il rumore che colpisce gli stop stretti
  e' modellato. Aspettativa BASSA visto il pattern monotono. Pre-registrare a parte.

## Lezione di metodo

Il gate formale (exp_R oraria) ha dato un falso positivo ("gap0.25 superiore"). Il
check di robustezza fuori-gate (gap 0.10/0.15) lo ha smascherato in un colpo. Senza
quel check si sarebbe "riaperto" e forse deployato un artefatto. I gate
pre-registrati non bastano se la MISURA che li alimenta e' viziata: qui la
granularita' oraria vizia sistematicamente la famiglia stop-stretto.
