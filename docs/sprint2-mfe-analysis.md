# Sprint 2 — Analisi MFE retention (asimmetria SL/TP)

Data: 2026-06-03. Campione: 15 trade chiusi Sprint 2 Fase 3 (id 39-53).

## Ipotesi sotto test

Intuizione di Andrea: il sistema protegge bene il rischio sul lato SL
(trailing + monitor che taglia le perdite) ma non ha un meccanismo
strutturato per consolidare il profit quando il prezzo va a favore e poi
ritraccia senza colpire il TP.

## Metodo (e suoi limiti)

Approccio leggero richiesto: usare gli eventi `monitoring_events` con
`event_type = trailing_sl` gia' in DB.

Per ogni trade:
- `profit_r_max` = massimo `profit_r` registrato negli eventi trailing,
  proxy del MFE in unita' di R.
- `risk_val` = `r_distance * size` (1R in valuta), da `details` evento.
- `pnl_R` = `pnl / risk_val`.
- `retention` = `pnl_R / profit_r_max`.

Limite importante della proxy. Il trailing campiona il `profit_r` solo
quando sposta lo SL, e lo sposta a soglie discrete (0.5R, 1.0R, 1.5R,
...). Quindi `profit_r_max` e' SOTTOSTIMATO rispetto al vero picco, e per
i trade usciti al TP l'ultimo `profit_r` loggato (~1.5R) e' molto sotto
il prezzo di uscita reale: in quei casi `retention` esce > 100%, che e'
un artefatto, non un give-back. La metrica e' interpretabile solo per i
trade che NON sono usciti al TP. Per una misura MFE vera servono gli
high/low delle candele durante la vita del trade (vedi Raccomandazioni).

## Tabella per trade

| id | asset | dir | profit_r_max | pnl_R | retention | exit_reason | note |
|----|-------|-----|-------------|-------|-----------|-------------|------|
| 39 | Nasdaq 100 | long | — | — | — | manual (monitor) | mai ≥0.5R, subito contro |
| 40 | Nasdaq 100 | long | 1.03 | +1.05 | 101% | manual (monitor) | win, uscito al picco |
| 41 | Gold | long | — | — | — | manual (monitor) | mai ≥0.5R |
| 42 | Brent Oil | short | 1.58 | +2.21 | 141%* | tp_hit | *artefatto proxy (TP) |
| 43 | Gold | long | — | — | — | stop_hit | mai ≥0.5R |
| 44 | Brent Oil | long | 0.60 | -0.50 | -83% | stop_hit | salvato a half-risk (-0.5R invece di -1R) |
| 45 | Nasdaq 100 | long | **1.02** | **0.00** | **0%** | stop_hit | **give-back totale: +1R → BE** |
| 46 | Brent Oil | short | — | — | — | manual (monitor) | mai ≥0.5R |
| 47 | Gold | short | — | — | — | stop_hit | mai ≥0.5R |
| 48 | Nasdaq 100 | long | 1.13 | +1.30 | 115%* | manual (monitor) | win |
| 49 | Brent Oil | short | 1.57 | +2.12 | 135%* | tp_hit | *artefatto proxy (TP) |
| 50 | Brent Oil | short | — | — | — | manual (monitor) | mai ≥0.5R |
| 51 | Brent Oil | long | **1.03** | **-0.02** | **-2%** | stop_hit | **give-back totale: +1R → BE** |
| 52 | Bitcoin | short | 1.54 | +1.99 | 129%* | tp_hit | *artefatto proxy (TP) |
| 53 | Gold | long | — | — | — | manual (monitor) | mai ≥0.5R |

`*` retention > 100% = artefatto della proxy (uscita al TP, picco reale non campionato).

## Sintesi aggregata

- **Media retention WIN** (40, 42, 48, 49, 52): **124%**. Valore > 100%
  perche' dominato dall'artefatto proxy: questi trade sono usciti al TP o
  vicino al picco, nessun give-back. Non informativo come retention.
- **Media retention LOSS con dati** (44, 51): **-43%**. Solo 2 dei 9 LOSS
  hanno superato 0.5R. Gli altri 7 (39, 41, 43, 46, 47, 50, 53) non hanno
  MAI raggiunto 0.5R: sono andati contro quasi subito, non sono casi di
  give-back ma di entry sbagliati / reattivita' (gia' oggetto della
  sprint2-reactivity-analysis).
- **Trade arrivati a profit_r ≥ 1.0 e poi NON consolidati**: **2 su 7**
  (id 45 e 51). Entrambi hanno toccato +1.0R di profit visibile e l'hanno
  restituito INTERAMENTE, uscendo a zero (BE).
- **Trade arrivati a profit_r ≥ 1.5 senza raggiungere il TP**: **0**.
  Tutti e 3 i trade che hanno superato 1.5R (42, 49, 52) sono arrivati al
  TP. Il give-back non avviene oltre 1.5R: avviene nella fascia 1.0-1.5R.

## Il sistema ha gia' un meccanismo "salva profit"? Risposta

Si', ma parziale, ed e' esattamente dove ha il buco che produce i casi
45 e 51.

Meccanismo esistente: **trailing SL R-multiple** (`_apply_trailing_stop`
in `src/position_monitor.py`), gira ogni 5 min H24 (`run_trailing_stops`)
piu' inline nel monitor da 30 min. Logica, solo migliorativa (lo SL non
torna mai indietro):

| profit raggiunto | SL portato a |
|------------------|--------------|
| ≥ 0.5R | entry -0.5R (half-risk) |
| ≥ 1.0R | entry (breakeven) |
| ≥ 1.5R | entry +0.5R |
| ≥ 2.0R | entry +1.0R |
| ... | +step_r per ogni step (step_r=0.5) |

Da qui l'asimmetria reale, e la conferma dell'intuizione:

1. **Lato SL la protezione e' immediata e continua**: il rischio massimo
   parte gia' fissato a -1R dall'apertura, e si stringe a -0.5R appena il
   trade tocca +0.5R.
2. **Lato profit il primo lock sopra il breakeven arriva solo a +1.5R**.
   Tutta la fascia **1.0R-1.5R ha lo SL ancorato a breakeven**. Un trade
   che culmina tra +1.0R e +1.4R e ritraccia esce a ZERO, restituendo
   tutto il profit visibile. E' precisamente cio' che e' successo a 45
   (+1.02R → 0%) e 51 (+1.03R → -2%).
3. **Nessuna logica TP-aware**: il trailing non si stringe avvicinandosi
   al TP, e non esiste un lock del profit legato al decadimento del
   momentum rispetto al picco.
4. **Il monitor LLM** (`_evaluate_position`) puo' chiudere su "momentum
   invertito" o "R:R residuo < 1:1", ma il system prompt e' esplicitamente
   conservativo (default HOLD) e non e' consapevole del picco di profit
   raggiunto. Non e' un salva-profit strutturato.

Conclusione: l'intuizione e' **confermata**, con una precisazione. Non
manca del tutto la protezione del profit (il trailing esiste e ha evitato
che 45 e 51 diventassero LOSS pieni); manca la protezione nella fascia
1.0-1.5R, dove sta il give-back. L'asimmetria e' di timing: lo SL e'
protetto da subito, il profit lo e' solo da 1.5R in su.

## Raccomandazioni (backlog Sprint 3 monitor)

1. **Chiudere il buco 1.0-1.5R**. Opzioni:
   - lock anticipato: a +1.25R → SL a +0.25R (consolida un quarto di R
     nella fascia oggi scoperta);
   - oppure ridurre `step_r` a 0.25 (lock piu' fitto sopra il BE).
2. **Trailing percentuale del MFE** oltre 1R: invece di soglie fisse,
   SL = entry + k * (picco_R - soglia), con k ~0.5. Aggancia il profit al
   picco reale invece che a gradini.
3. **Misurare il vero MFE**. Questa analisi e' limitata dalla proxy degli
   eventi trailing. Per Sprint 3 conviene loggare ad ogni ciclo monitor
   il `high`/`low` raggiunto (o ricostruirlo dalle candele alla chiusura)
   cosi' MFE retention diventa una metrica affidabile e non sottostimata.
4. Il problema dominante nel campione resta comunque a monte: 7 LOSS su 9
   non hanno mai superato 0.5R. Il give-back (45, 51) e' reale ma riguarda
   2-3 trade; l'ottimizzazione dell'entry/reattivita' ha priorita'
   numerica maggiore del salva-profit.
