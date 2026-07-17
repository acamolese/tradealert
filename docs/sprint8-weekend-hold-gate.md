# Sprint 8 Fase 1 — Tenere posizioni attraverso il weekend: costa o è solo varianza?

Data: 2026-07-17. Stato: **PRE-REGISTRATO prima di calcolare gli esiti.**
Analisi offline, sola lettura. Le soglie di gate qui sotto sono fissate ORA e non
si spostano dopo aver visto i numeri (regola anti-HARKing del progetto).

## Origine

1. Richiesta utente (17/07): valutare se chiudere i setup prima del weekend in
   base al rischio, con l'idea di un "sistema del venerdì" che decide tenere/chiudere.
2. Evento scatenante: Brent long #146 aperto il 17/07 durante l'escalation
   USA-Iran (rischio Hormuz, ultimatum su infrastruttura energetica nel weekend).
3. Finding collaterale già registrato in `docs/sprint5-weekend-gap.md`: il rischio
   gap-weekend è **generale e massimo su Brent** (p90 1.50R), ma lo slippage reale
   storicamente subito è ~0. Quel documento chiedeva esplicitamente, se si vuole
   affrontarlo, di farlo come **regola generale pre-registrata a parte con il suo
   gate**. Questo è quel gate.

## Domanda pre-registrata

Tenere una posizione attraverso la chiusura settimanale del suo mercato:
1. **costa expectancy** (i gap di weekend sono mediamente contro la posizione), o
2. **è neutro in media ma aggiunge varianza di coda** (gap random in direzione,
   ma con code grandi su asset come Brent), o
3. **è irrilevante** (gap piccolo, nessun problema)?

E se costa o rischia: una **regola deterministica** di chiusura venerdì
pre-close sugli asset gap-prone la recupera, o taglia i winner come la
lezione dell'uscita a euro fisso (`docs/sprint5-fixed-euro-exit.md`)?

## Due policy distinte (non confonderle)

- **Policy SALTA-GAP**: chiudo venerdì al close settimanale, riapro lunedì
  all'apertura. Neutralizza SOLO il gap, mantiene il trade. Isola l'effetto-gap.
  Costo di attrito: doppio spread + eventuale slippage di riapertura.
- **Policy ESCI-E-BASTA**: chiudo venerdì e non riapro (è ciò che l'utente ha
  proposto per il Brent). Neutralizza il gap MA rinuncia alla traiettoria di
  lunedì+. Operativamente semplice (una sola azione), ma può tagliare i winner
  che continuano dopo il weekend.

## Metodo (un run, candele HOUR Capital, sola lettura)

### Parte A — Struttura di mercato (spina dorsale, n-independent)

Riuso di `jobs/weekend_gap_analysis.py`: distribuzione dei gap di weekend per
asset in R-equivalente (n, mediana, p90, max). Già disponibile per Brent/Gold/
Hang Seng/Nikkei/Bitcoin; da estendere a Nasdaq/US500. Questa parte quantifica
il **rischio di coda strutturale** ed è indipendente dal numero di trade.
Assunzione neutra dichiarata: in assenza di un bias direzionale dimostrato, il
gap contribuisce ~0 all'expectancy attesa e la sua ampiezza (p90/max) è puro
rischio di coda bidirezionale.

### Parte B — Trade reali tenuti attraverso il weekend

Campione (dimensionato PRIMA degli esiti, solo conteggi): 30 trade held-weekend
in tutta la storia, di cui **14 su asset gap-prone** (Gold 5, Brent 4, Hang Seng
3, Nasdaq 2); gli altri 16 sono crypto (24/7, gap strutturale ~0) e si
**escludono**. Recuperabili con candele HOUR (~ultimi 40-60gg) circa 7 weekend
recenti (Gold #59/#78/#90, Hang Seng #89/#103/#118, Brent #119). n piccolo,
dichiarato: la Parte B è **indicativa**, il peso probatorio lo porta la Parte A.

Per ciascun trade gap-prone held-weekend, dalle candele HOUR dell'asset attorno
al weekend attraversato:
- prezzo di **chiusura venerdì** (ultima candela pre-gap) e **riapertura**
  (prima candela post-gap);
- **M1 — gap direzionale** = (riapertura − chiusura)/`r_dist` · segno(direzione),
  in R. È il P&L che la Policy SALTA-GAP eviterebbe (se <0) o perderebbe (se >0);
- **M2 — expectancy del "tenere dopo venerdì"** = R_reale − R_al_close_venerdì.
  È ciò che la Policy ESCI-E-BASTA rinuncia/evita: gap + traiettoria di lunedì+.

`r_dist` = entry · stop%/100 dal signal del trade (come negli altri gate in R).

## Gate PRE-REGISTRATO (soglie fissate prima dei numeri)

Definizioni: `E_tieni` = expectancy media di M2 sugli asset gap-prone (positivo =
tenere dopo venerdì guadagna); `Tail_p90` = p90 avverso di |M1| sugli asset
gap-prone dalla Parte A; confronto con l'attrito stimato (doppio spread).

| Verdetto | Condizione | Azione |
|---|---|---|
| **REGOLA SU EXPECTANCY** | `E_tieni` ≤ −0.10R **e** robusto (senza il singolo trade peggiore resta ≤ −0.05R) **e** M1 medio ≤ −0.05R (il danno è nel gap, non nella traiettoria) | Fase 2: regola deterministica di chiusura venerdì pre-close sugli asset gap-prone. Zero LLM. |
| **SOLO RISCHIO-CODA** | `E_tieni` neutro (\|media\| < 0.10R) **ma** `Tail_p90` ≥ 0.5R su Brent/asset gap-prone | Nessun edge. Opzione di riduzione-varianza da decidere ESPLICITAMENTE dall'utente (chiudere accetta di rinunciare a expectancy ~0 per tagliare la coda). Non auto-deploy. |
| **NON GIUSTIFICATO** | `E_tieni` neutro **e** `Tail_p90` < 0.5R | Archiviare. #146 resta un override una-tantum, nessuna regola. |

Nota di coerenza: se il danno M2 fosse concentrato nella **traiettoria di lunedì**
e non nel gap (M1 ~0 ma E_tieni negativo), NON è un problema di gap-weekend ma di
durata/gestione del trade → si tratta altrove, non con la chiusura del venerdì.

## Robustezza pre-registrata

- Escludere le crypto (gap ~0) dagli asset gap-prone: la regola, se emerge, vale
  solo per gli asset che chiudono davvero (energia, metalli, indici).
- Isolare l'outlier **#89 Hang Seng** (−1.99R, il caso scatenante di sprint5,
  causa = ingresso in sessione sottile fuori-orario, non gap tipico): riportare i
  numeri con e senza #89. Un verdetto che dipende da #89 non è un verdetto.
- Separare M1 (gap) da M2 (gap + traiettoria) per non attribuire alla chiusura
  del venerdì un danno che è di continuazione.

## Limiti

Candele HOUR (~40-60gg, limite API): i weekend di aprile-maggio non sono
ricostruibili, la Parte B copre i recenti. n gap-prone piccolo (~7 recuperabili)
→ Parte B indicativa, non significativa. Gap misurati sui mid, senza spread.
`r_dist` da stop% del signal. Direzioni e ordini di grandezza, non test statistici.

## Perché NON si parte dall'LLM geopolitico

L'idea di un valutatore geopolitico del venerdì è la Fase 3, subordinata a due
prove: (1) la Fase 1 mostra che il rischio-gap è materiale; (2) una regola
deterministica semplice (Fase 2) lo cattura. L'LLM geopolitico va poi giustificato
come miglioramento MISURABILE sopra la regola deterministica, allo stesso standard
con cui lo score LLM in ingresso è stato bocciato (corr ≈ 0, `docs/sprint7-fase-b.md`):
il mercato prezza già la geopolitica nota, e un valutatore prudente rischia di
chiudere troppo spesso tagliando i winner.
