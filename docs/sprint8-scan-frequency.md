# Sprint 8 — La frequenza di scan aiuta l'ingresso? (pre-registrato)

Data: 2026-07-22. Stato: **PRE-REGISTRATO prima di calcolare gli esiti.**
Soglie di gate fissate ORA, non si spostano dopo aver visto i numeri (anti-HARKing).

## Origine

Idea utente (2026-07-22): ora che l'ingresso e' deterministico (`SCORING_LLM_OFF`,
zero token IA per scan), scannerizzare piu' spesso costa quasi nulla. Ipotesi:
scan piu' frequente → si entra "in momenti piu' propizi" (prima nel movimento,
non a corsa gia' fatta). Oggi lo scan e' orario (cron :05).

Dato osservato che motiva il test (non ancora un verdetto): il "mover piu' forte"
cambia il 46% delle volte tra due scan orari consecutivi, e all'ingresso il
movimento del giorno e' gia' esteso (|dpc| mediano 2.53%, 75% degli scan > 2%).
C'e' quindi spazio teorico per entrare prima; resta da vedere se paga.

## Perche' ORA e' testabile (e prima no)

Il selettore e' deterministico e riproducibile: si puo' backtestare la frequenza
di scan in modo ESATTO sui dati storici. Con l'LLM in ingresso non si poteva
(non riproducibile). Questa e' la leva che rende la domanda finalmente decidibile.

## Due effetti distinti (non confonderli)

Scan piu' frequente agisce su due canali indipendenti:

- **E1 — timing**: sullo STESSO segnale, entri prima (meno ritardo tra formazione
  del segnale e fill). Riduce il "chasing" temporale.
- **E2 — segnali nuovi**: cogli mover che spiccano e rientrano DENTRO l'ora e che
  lo scan orario non vede mai (es. spike alle 10:23, sparito alle 11:05).

## Vincolo dati (determina la struttura del test)

I candle storici locali sono **HOUR e DAY** (`data/candles/`, 2020→2026-07-14),
niente intra-ora. Quindi:
- **E1 e' testabile subito** e in modo robusto su tutto lo storico (barre orarie).
- **E2 richiede candle fini** (MINUTE_15/5) da scaricare da Capital, disponibili
  solo per un periodo recente e breve → risultato indicativo, un solo regime.

Struttura obbligata in due fasi, la seconda condizionata alla prima.

## FASE 1 — Effetto timing/ritardo (questo documento, dati orari)

Ricostruisce il selettore v1-momentum su barre orarie: a ogni barra t, per ogni
asset del paniere live (11 epic) calcola dpc = (mid_close(t) − chiusura giorno
prec.)/chiusura · 100, spread% = (ask−bid)/mid, atr_pct. Filtri live: spread ≤
0.5%, |dpc| ≥ 0.5%. Sceglie il top |dpc| eligibile non in dedup (asset+direzione,
24h). Stop = max(1.5·atr%, 0.5%), target = 2·stop (regola v1 invariata).

Variabile del test: **ritardo di ingresso D** ∈ {1, 2, 3, 4} barre orarie tra il
close del segnale e il fill. D=1 = scan orario ideale (entri la barra dopo); D
crescente = scan che ha "mancato il momento" ed entra piu' tardi. Frequenza scan
maggiore ⇒ ritardo minore, quindi D=1 e' il proxy della frequenza alta.

Uscita: motore live gia' validato (M3: SL a k·ATR, TP a rr·SL, trailing D+V1+V2),
riuso di `jobs/backtest_run.py` (spread bid/ask reale, overnight fee, max hold 30g,
cooldown 24h). Metrica: **expectancy_R netta costi**.

## Gate PRE-REGISTRATO Fase 1

Sia `exp(D)` l'expectancy_R netta al ritardo D.

| Verdetto | Condizione | Azione |
|---|---|---|
| **TIMING CONTA** | `exp(1) − exp(3) ≥ +0.10R` **e** monotòno (exp(1)≥exp(2)≥exp(3)) **e** robusto (senza i 2 migliori trade di D=1 il vantaggio resta ≥ +0.05R) | Il ritardo degrada l'esito → la frequenza puo' aiutare. Procedi a FASE 2 (dati fini, quantifica il guadagno reale a 15/30 min). |
| **TIMING IRRILEVANTE** | `|exp(1) − exp(3)| < 0.05R` (curve piatte) | Entrare prima non cambia l'esito → l'ipotesi "momenti piu' propizi" e' falsificata sul canale timing. STOP: NON scaricare dati fini, NON toccare il cron. E2 resta non testato (dichiarato). |
| **INDECISO** | tutto il resto | Indicativo. Decide l'utente se passare a Fase 2. |

Coerenza attesa: il test "chasing" (archiviato NEGATIVO, `docs/sprint4-chasing-validation-setup.md`)
ha gia' trovato che l'ESTENSIONE del movimento all'entry non predice l'esito. Il
ritardo temporale e' correlato ma non identico; se anche qui esce piatto, e' una
seconda conferma indipendente che l'ingresso non ha leva di timing.

## FASE 2 — Effetto segnali nuovi (SOLO se Fase 1 = TIMING CONTA)

Scarica candle MINUTE_15 del paniere per il periodo recente disponibile, simula
scan a 15/30/60 min con lo stesso motore, confronta expectancy_R netta E numero
trade (piu' scan = piu' aperture = piu' spread pagato: il gate dovra' essere sul
NETTO). Pre-registrazione a parte prima di eseguirla.

## Costi e limiti

Costi nel motore: spread reale bid/ask, overnight fee (0.025%/notte, 0.06% BTC).
Limiti Fase 1: dpc ricostruito (chiusura giorno prec.) ~ ma non identico al
`percentageChange` live di Capital; una posizione per volta per asset (dedup);
E2 non catturato. Direzioni e ordini di grandezza, robusto sul lungo periodo.
