# Sprint 6 A1 — Replay controfattuale delle chiusure del monitor LLM

Data esecuzione: 2026-07-02. Script: `jobs/monitor_close_replay.py` (sola lettura).
Gate pre-registrato in `docs/sprint6-piano-scalata.md` PRIMA di guardare i numeri.
Dettaglio per-trade: `docs/sprint6-monitor-replay.csv`.

## Verdetto: NEUTRO (con lettura sostanziale: il monitor NON distrugge valore, semmai ne aggiunge)

| Condizione del gate | Soglia | Osservato | Esito |
|---|---|---|---|
| DANNOSO: delta_r medio | ≥ +0.15R | **-0.127R** | no |
| UTILE: delta_r medio | ≤ -0.15R | -0.127R | no (vicino) |
| UTILE: senza i 2 peggiori cf | ≤ -0.10R | -0.091R | no |

delta_r = exit_R controfattuale (solo bracket SL trailato/TP, sim oltre il closed_at
reale, cap 10gg) − exit_R reale. Il verdetto formale è NEUTRO perché la media
manca la soglia UTILE per 0.023R; ma la direzione è inequivocabile e OPPOSTA
all'ipotesi di partenza.

## Copertura

- Trade chiusi dal monitor (proposta accettata + auto-close): 55.
- Simulati: **51**. Scartati 4 (#4, #5, #6 senza signal collegato; #9 senza candele) — nessun cap silenzioso.
- Mark-to-market all'orizzonte +10gg: 3 casi (#92, #99, #100).
- Risoluzione candele: 5m per 50 trade su 51 (uno a 30m).

## Numeri

| Taglio | n | delta_r medio | mediana | R reale | R controfattuale |
|---|---|---|---|---|---|
| Sample completo | 51 | **-0.127** | **-0.312** | +1.29 | -5.20 |
| Senza i 2 migliori cf | 49 | -0.204 | -0.319 | +1.33 | -8.69 |
| Senza i 2 peggiori cf | 49 | -0.091 | -0.281 | +0.03 | -4.45 |
| Sottogruppo proposte accettate | 36 | -0.046 | -0.255 | +1.49 | -0.19 |
| Sottogruppo auto-close | 15 | **-0.321** | -0.421 | -0.20 | -5.01 |
| Long | 34 | -0.095 | -0.296 | +2.01 | -1.22 |
| Short | 17 | -0.192 | -0.397 | -0.72 | -3.98 |

## Lettura

1. **L'ipotesi "il monitor taglia i runner" è falsificata su questo campione.**
   La mediana fortemente negativa (-0.31R) dice che nel caso tipico il monitor
   chiude MEGLIO del bracket: esce a -0.1/-0.6R su trade che, lasciati correre,
   sarebbero finiti allo SL pieno (-1R). Il baseline "bucket manual = -€15.50"
   era una lettura di sopravvivenza: i trade arrivati a stop_hit erano peggiori,
   non migliori.
2. **Il costo del monitor sono i pochi controfattuali grandi**: #72 Brent short
   (+1.83R lasciato sul tavolo), #39/#70 Nasdaq long (+1.70/+1.46R), #69 Bitcoin
   (+1.48R), #26 US500 (+1.28R). Cinque casi non compensano i ~30 loser
   accorciati, ma tengono la media sopra la soglia UTILE.
3. **L'auto-close (live dal 2026-06-23) è il sottogruppo migliore** (-0.32R medio
   a favore del monitor): coerente col prompt conservativo default-HOLD.
4. Caveat dichiarati: SL-first e cap 10gg remano A FAVORE del monitor (i bias
   spingono verso "utile"), quindi il NEUTRO formale è la lettura corretta; il
   segnale sostanziale resta che il monitor non va toccato.

## Azione (da matrice pre-registrata)

**NEUTRO → nessuna modifica a produzione.** `MONITOR_CLOSE_ENABLED` non viene
introdotto, l'auto-close resta attivo, la cadenza monitor resta 30 minuti.
Thread archiviato: non ri-esplorare prima di **30 nuovi trade chiusi dal monitor**
(oggi n=51; trigger a n≥81).

Implicazione per A2 (gate long): il bleed del lato long NON è un artefatto del
monitor (nel controfattuale bracket-only i long peggiorano: +2.01R reale vs
-1.22R cf). L'analisi A2 procede sul ramo selezione.
