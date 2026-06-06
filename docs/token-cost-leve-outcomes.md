# Ottimizzazione costo token — esiti Leva 1, Leva 3, analisi Leva 2

Data: 2026-06-06. Seguito di `docs/token-cost-analysis.md`. **Nessuna
modifica deployata**: entrambe le leve operative si sono rivelate non
applicabili come previsto (test pre-deploy fallito per la Leva 1, finestra
già spenta per la Leva 3). Sotto i dati. Il monitor non è stato toccato.

## Leva 1 (output trim) — TEST FALLITO, non deployata

### Intervento previsto
Far generare allo scanner la thesis discorsiva solo per le proposte con
score >= 7 (soglia di signal), lasciando le altre compatte. Vincolo: non
deve cambiare lo score né la decisione di apertura.

### Struttura attuale verificata
Una sola chiamata Sonnet restituisce top-3 proposte con score + thesis +
key_factors + risks insieme. Lo score è emesso nel JSON PRIMA della thesis,
quindi in teoria la thesis è giustificazione post-hoc.

### Test (temp=0 per isolare l'effetto prompt dal jitter di inferenza)
Confronto score vecchio vs nuovo prompt su input storici reali, con
controllo old-vs-old per separare l'effetto del prompt dal rumore.

Due varianti provate:
1. Riscrittura del blocco output → drift score osservato.
2. Intervento minimale (sezione scoring byte-identica, sola clausola finale
   di omissione) → **drift ancora presente**.

Risultato decisivo (test multi-asset stabile, 3 run per prompt):

| asset | OLD ×3 | NEW ×3 | self-jitter | drift |
|-------|--------|--------|-------------|-------|
| Gold | 6.5, 6.5, 6.5 | 7.0, 7.0, 7.0 | 0.0 | **+0.5** |
| Nasdaq 100 | 5.5 ×3 | 5.5 ×3 | 0.0 | 0.0 |
| Brent Oil | 7.5 ×3 | 7.5 ×3 | 0.0 | 0.0 |

Gold: il prompt OLD dà 6.5 stabile su 3 run, il NEW dà 7.0 stabile su 3 run.
Self-jitter zero → **non è rumore, è un effetto reale del prompt**. E il
+0.5 attraversa la soglia 7: un no_setup diventerebbe un signal.

### Conclusione
Anche la sola menzione di "score >= 7" nel system prompt sposta la
calibrazione dello scoring di ~+0.5 sui casi borderline, in modo stabile e
ripetibile. Per il gate esplicito ("se lo score cambia, va corretto") la
Leva 1 in forma single-call **non è deployabile**: cambierebbe le decisioni
di apertura. Non deployata.

### Perché non è banalmente correggibile
Per non generare la thesis il modello va istruito sul fatto che è "sotto
soglia", e quel riferimento alla soglia nuda anchora lo scoring. Una
versione davvero score-neutrale richiede di **separare scoring e thesis in
due chiamate** con una pass di scoring ricalibrata e ri-validata: di fatto
converge con il design two-tier della Leva 2 e va validata insieme in
Sprint 4, non deployata di corsa.

## Leva 3 (cadenza notturna 21-03 UTC) — NON APPLICABILE, nessuna modifica

Verifica TZ della VM: `Europe/Rome (CEST, +0200)`. Il crontab dichiara in
header "tutti gli orari in TZ Europe/Rome". Riga scanner:
`5 7-22 * * * ... morning_scan` = **07:00-22:00 Roma = 05:00-20:00 UTC**.

Quindi la fascia 21:00-03:00 UTC (23:00-05:00 IT) **è già completamente
non scansionata**: lo scanner si ferma alle 22:00 Roma (20:00 UTC) e
riparte alle 07:00 Roma (05:00 UTC). Confermato dai dati: gli scanner_runs
hanno `ran_at` solo fra le 05 e le 20 UTC, mai 21-04.

Non c'è nulla da diradare nella finestra indicata: **risparmio 0, nessuna
modifica al crontab**. Le ore davvero "morte ma scansionate" (05, 06, 10,
11, 13, 17, 18 UTC, 0 signal in 14 giorni) sono DENTRO la finestra attiva,
ma il vincolo esplicito vietava di toccare fuori da 21-03 UTC, quindi sono
rimaste invariate. Se in futuro si vuole un risparmio reale di cadenza, il
target corretto sono quelle ore in-window (da decidere a parte).

## Leva 2 (screen Haiku → Sonnet) — SOLO ANALISI

Domanda: uno screen Haiku che scarta i candidati deboli prima di Sonnet
preserva i signal veri (recall)? Misurato su 25 signal storici (id 85-109,
tutti Sonnet >= 7 al tempo) ri-scorati con Haiku (claude-haiku-4-5) sulle
stesse feature, temp=0.

Haiku score sui 25 signal: **mediana 6.8, min 4.2, max 7.8**. Haiku scora
sistematicamente più basso e in modo irregolare (Sonnet 7.0 → Haiku 4.2 su
sig 88, 97, 104).

Recall di uno screen Haiku alle varie soglie:

| soglia screen Haiku | signal mantenuti | recall |
|---------------------|------------------|--------|
| >= 7.0 | 5/25 | 20% |
| >= 6.5 | 13/25 | 52% |
| >= 6.0 | 16/25 | 64% |
| >= 5.5 | 16/25 | 64% |
| >= 5.0 | 21/25 | 84% |

Per non scartare i signal veri lo screen andrebbe a ~5.0, e perde comunque
il 16%. Per recall >95% servirebbe soglia ~4.0, che lascerebbe passare
quasi tutto a Sonnet → risparmio quasi nullo. Haiku non condivide la scala
di Sonnet: uno screen a soglia "7" (come Sonnet) taglierebbe l'80% dei
setup buoni.

### Conclusione analitica (Leva 2)
Uno screen Haiku come semplice filtro a soglia di score **non è viabile**:
o taglia troppi setup veri (soglia alta) o non risparmia (soglia bassa). Un
two-tier sensato richiederebbe di tarare Haiku sulla SUA scala (es. "Haiku
passa a Sonnet tutto ciò che scora >= X dove X è calibrato sui suoi
percentili, non su 7") oppure un pre-filtro deterministico, e una
validazione di recall su un campione più grande. Da progettare in Sprint 4.

### Caveat
Harness single-asset rumoroso (in isolamento anche Sonnet jitter, vedi
Leva 1), 1 run per signal, n=25, solo casi signal_sent (niente misura di
precisione/savings sui no_setup, che richiederebbe input no_setup salvati).
Tutto preliminare.

## Esito complessivo

Nessuna delle due ottimizzazioni operative è stata applicata: la Leva 1
viola il gate sullo score, la Leva 3 agisce su una finestra già spenta. Il
costo resta ~$0.53/giorno. Le strade reali di risparmio (corretto output
trim a due chiamate; screen Haiku ricalibrato; trim delle ore morte
in-window) richiedono tutte una fase di validazione e sono candidate per
Sprint 4. Il monitor LLM resta invariato come da vincolo.

## Aggiornamento 2026-06-06 — Leva 1 Proposta 3 (thesis corta per tutti)

Tentativo a rischio minimo: ridurre la thesis da "3-5 righe" a "massimo 2
righe asciutte", senza condizionali e senza nominare la soglia, accorciando
anche l'esempio few-shot Brent (era ~5 righe, anchor di lunghezza). Sezione
di scoring byte-identica.

Gate (temp=0, 2 universi da 3 asset, 3 run per prompt, Gold incluso):

| asset | OLD ×3 | NEW ×3 | self-jitter | drift | esito |
|-------|--------|--------|-------------|-------|-------|
| Gold | 6.5,6.5,6.5 | 6.5,6.5,6.5 | 0.0 | 0.0 | PULITO |
| Brent Oil | 7.5,7.5,7.5 | 7.5,7.5,7.5 | 0.0 | 0.0 | PULITO |
| Nasdaq 100 | 5.5,5.5,5.5 | 5.5,6.0,5.5 | 0.0 | 0.5 | DRIFT |
| US500 | 6.5,6.5,6.0 | 6.5,6.5,6.5 | 0.5 | 0.5 | PULITO |
| Bitcoin | 6.0,6.0,6.5 | 4.0,6.0,6.0 | 0.5 | 2.5 | DRIFT |

Output risparmiato ~22% (out/call 1440 -> 1130).

VERDETTO: NON DEPLOYATA. Bitcoin drift 2.5 fuori dal self-jitter, Nasdaq
0.5 oltre il jitter zero. Per il gate ("drift fuori -> fermarsi") non si
deploya.

Nota interpretativa (non cambia il verdetto): a differenza del +0.5 STABILE
di Gold del tentativo precedente (6.5x3 -> 7.0x3, sistematico), qui i drift
sono OUTLIER singoli su 3 run (Nasdaq 2/3 invariati, Bitcoin 2/3 invariati):
sembrano jitter, non shift sistematici. Inoltre cadono su asset SOTTO la
soglia 7 (Nasdaq 5.5-6, Bitcoin 4-6.5): nessuno attraversa la soglia, quindi
la DECISIONE di apertura non cambierebbe. Gli unici asset vicini/sopra la
soglia (Gold 6.5, Brent 7.5) sono perfettamente stabili (drift 0). La
thesis corta sembra pero' aumentare leggermente la varianza di scoring sugli
asset deboli. Per prudenza, e per rispettare il gate alla lettera, si
rimanda tutto a Sprint 4 (Leva 1 corretta a due chiamate + Leva 2).
