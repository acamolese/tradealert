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
