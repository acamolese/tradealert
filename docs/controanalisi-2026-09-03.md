# Controanalisi TradeAlert (stato al 2026-09-03)

Perimetro: l'unico sistema vivo, il grid2 bidirezionale sui due conti (6 profili
reali, 8 demo, cron ogni 10 minuti), più il livello di comunicazione Telegram
(riepilogo orario, avvisi, comandi del listener, health check, settimanale).
Fonti: codice in `jobs/grid2.py`, `src/grid_net.py`, `src/grid_profit.py`,
`src/grid_report.py`; `.env` e crontab sulla VM; log `g2r.log`/`g2d.log` dal
22/08; transazioni del broker degli ultimi 13 giorni su entrambi i conti.

## 0. Sintesi

1. **Bug critico, attivo oggi**: alle 19:07 del 3/9 il conto demo è stato
   messo in pausa da uno "stop perdita di -976,83 €" inesistente. La lettura
   del saldo è tornata vuota (timeout di rete), l'equity è stata interpretata
   come 0,00 € e il job ha chiuso la posizione DE40 (-1,43 €) e fermato tutti
   i grid demo. Il conto reale ha la stessa falla: con stop a -6 € e floor a
   35 €, una singola lettura vuota chiude tutto e ferma il sistema.
2. **Il grid non incassa il passo**: il passo dichiarato è 3 %, ma l'80 % dei
   fill consecutivi sullo stesso strumento dista meno dello 0,15 % dal
   precedente. Su 250 coppie reali una sola supera l'1 %. È "chattering" sul
   confine del gradino zero, non un grid. Risultato dei trade su 13 giorni:
   reale -0,03 € su 129 movimenti, demo +1,38 € su 174. I costi overnight
   (-0,40 € reale) pesano più dei trade.
3. **Il backtest che ha validato la configurazione girava su barre giornaliere**
   (`jobs/grid_anchor_test.py`, `resolution="DAY"`), il cron gira ogni 10
   minuti: la simulazione non poteva vedere il chattering. Prima di ogni
   ritocco serve una simulazione alla stessa frequenza del cron.
4. **Comunicazione**: il riepilogo orario è tecnicamente onesto ma parla la
   lingua del broker (equity, flottante, realizzato, mosse, baseline) e dà tre
   numeri di P&L diversi che il lettore deve riconciliare. Arriva 24 volte al
   giorno, anche di notte e nel weekend a mercati chiusi. Il sistema può
   fermarsi da solo, ma l'utente non ha un comando Telegram per farlo ripartire.

## Parte A. Strategia e funzionamento

### A1. Fail-open sulla lettura del conto (P0)

`jobs/grid2.py` legge `acc = (get_account_info().get("accounts") or [{}])[0]`
e poi `equity = equity_conto(bal)`, che restituisce 0,0 quando il blocco
`balance` manca. Da lì in poi tutte le protezioni scattano contro un conto
"vuoto":

- `perdita = baseline - equity` → 976,83 € → stop di perdita, chiusura
  posizioni, `bloccato = true` per l'intero conto;
- `equity < kill_equity_eur` → kill switch per profilo;
- lo stesso stato viene mostrato dal report come "⏸ IN PAUSA" senza motivo.

Evidenza: `logs/g2d.log` riga 21971 (`STOP PERDITA: -976.83€, chiuse 1
posizioni`) preceduta da due `ReadTimeout` sul login nei minuti precedenti;
`data/g2_profit_demo.json` ha `bloccato_a: 0.0`. Oggi il log demo conta dieci
traceback per timeout: la rete verso Capital è instabile e l'evento si ripeterà.

Fix (poche righe, deterministico, senza cambio di strategia):

- se `accounts` è vuoto, `balance` manca o `equity <= 0`: log di errore,
  nessuna decisione, uscita dal run. Nessuna protezione deve scattare su un
  dato mancante (fail-closed sull'azione, non sul capitale);
- sanity check sulle soglie: una "perdita" superiore al 50 % della baseline in
  un singolo run è per costruzione un errore di lettura, non un evento di
  mercato (il kill per profilo scatta molto prima);
- stesso guard in `src/grid_report.py`, così il riepilogo non mostra 0,00 €;
- un avviso Telegram deduplicato ("non riesco a leggere il conto, riprovo")
  al massimo una volta l'ora.

Dopo il fix va sbloccato il demo (`--riparti` con `CAPITAL_ENV=demo`), sapendo
che azzera la riga "dall'avvio" del demo. Scelta dell'utente.

### A2. Chattering sul confine del gradino (P0 strategico)

Meccanica attuale (`src/grid_net.py`): `target = -floor(log(p/p0)/log(1+s))`.
Il gradino 0 è la banda [p0, p0·1,03): dentro si sta flat. Appena il prezzo
scende sotto p0 di un centesimo il livello diventa -1 e il grid compra; appena
risale sopra p0 vende. Le due soglie coincidono: **compra a p0 e vende a p0**.
Il passo del 3 % protegge solo dall'accumulo della seconda unità, ma la prima
scatta a distanza zero. In più p0 è una EMA5 che include la barra giornaliera in
corso, quindi si sposta a ogni run e insegue il prezzo.

Misura sui log dal 22/08 (fill consecutivi sullo stesso strumento):

| conto | coppie di fill | a meno di 0,15 % | oltre 1 % | mediana |
|---|---|---|---|---|
| reale | 250 | 197 (79 %) | 1 | 0,04-0,09 % |
| demo | 335 | 277 (83 %) | 1 | 0,03-0,10 % |

Esempio reale di oggi su NL25: vende 1104,40, compra 1104,45, vende 1104,30,
compra 1104,03, vende 1104,80, compra 1103,65. Sono giri da 0,01-0,05 % che
pagano lo spread e incassano rumore.

Risultato dal broker, ultimi 13 giorni:

| conto | trade | n | vincenti | perdenti | media vinc. | media perd. | overnight | dividendi |
|---|---|---|---|---|---|---|---|---|
| reale | -0,03 € | 129 | 88 | 17 | +0,03 € | -0,16 € | -0,40 € | +0,16 € |
| demo | +1,38 € | 174 | 135 | 34 | | | -0,93 € | +0,31 € |

La firma è quella tipica del "raccogliere centesimi": molte vincite minuscole
(spread e rumore), poche perdite cinque volte più grandi (quando il prezzo si
allontana davvero). L'atteso netto è zero e i costi di detenzione lo portano
sotto: -0,24 € netti in 13 giorni sul reale equivalgono a circa il 13 % annuo
dell'equity, tutto costo certo.

Cosa vale davvero un gradino del 3 % sul reale (nozionale di una unità dal
dry-run di stasera):

| strumento | unità | nozionale | un giro pieno (3 %) |
|---|---|---|---|
| NL25 | 0,01 | 12,8 € | 0,38 € |
| US100 | 0,001 | 29,5 € | 0,88 € |
| HK50 | 0,01 | 32,4 € | 0,97 € |
| J225 | 0,1 | 41,5 € | 1,24 € |
| GOLD | 0,01 | 44,7 € | 1,34 € |
| US30 | 0,001 | 53,7 € | 1,61 € |

Oggi i giri hanno reso +0,00 / +0,01 €: il sistema incassa un centesimo dove
il disegno prometteva un euro.

Correzione proposta (isteresi da grid classico, senza file di stato: la
posizione del broker resta lo stato):

- con `u` unità long: aggiungi una unità solo se `p < p0·(1+s)^-(u+1)`,
  riduci solo se `p > p0·(1+s)^-(u-1)`; simmetrico per lo short;
- quindi si compra sul livello inferiore e si vende su quello superiore:
  la prima unità entra a -3 % e esce a p0, un giro vale un passo intero;
- ancoraggio calcolato sulle sole barre chiuse (escludere la barra del giorno
  in corso): i livelli restano fermi per tutta la giornata invece di muoversi
  ogni 10 minuti;
- se con isteresi piena i giri diventano troppo rari, si abbassa il passo
  (1-1,5 %) e non l'isteresi: il backtest giornaliero dava EMA5 + 1 % a 73 %
  di finestre positive, e il chattering sparisce comunque.

Nessuna di queste scelte va in produzione senza il punto A3.

### A3. La simulazione deve girare alla frequenza del cron (P1, propedeutico)

`grid_anchor_test.py` e `grid_drift_bias.py` usano barre DAY. A quella
risoluzione un round-trip al confine del gradino è invisibile, e infatti
"82 % di finestre positive" non descrive quello che il conto ha fatto. Serve:

- replay della funzione pura `pianifica_net` su barre 10/15 minuti degli
  ultimi 200-400 giorni per gli 8 strumenti, con spread reale, con l'EMA
  calcolata esattamente come nel job (barra corrente inclusa vs esclusa);
- confronto a parità di dati: configurazione attuale vs isteresi piena vs
  isteresi + passo 1 %, 1,5 %, 2 %;
- metriche: giri/mese, valore medio del giro, % finestre positive, peggior
  drawdown, costi (spread + overnight) in euro alla taglia reale.

L'harness esiste già (`jobs/grid_backtest.py`, `jobs/backtest_run.py`): è
lavoro di adattamento, non da zero.

### A4. Costi di detenzione e taglia (P1)

Sul reale i costi overnight sono la voce più grande del conto (-0,40 € in 13
giorni contro -0,03 € di trade). Il grid tiene inventario a lungo per
costruzione, quindi paga swap ogni notte anche quando non gira. Due leve
deterministiche, coerenti con il piano di capitalizzazione del 17/08:

- non tenere inventario nei giorni in cui il grid non ha un gradino da
  incassare (con isteresi piena la posizione media sarà comunque più vicina a
  zero);
- escludere gli strumenti dove lo swap è sproporzionato alla taglia (HK50 e
  J225 pagano 0,10 € ciascuno su nozionali di 30-40 €).

### A5. Correlazione del paniere (P2)

Cinque dei sei profili reali sono indici azionari fortemente correlati
(US100, US30, NL25, J225, HK50). Il grid non prevede la direzione, ma in un
giorno di trend forte tutti e cinque vanno al tetto insieme e il kill per
profilo (4 €) si somma. Con max 2 unità è contenuto (esposizione massima
totale ~430 € su 50 € di conto, leva ~8,5x). Vale la pena avere una
protezione sul totale dell'esposizione, non solo sul P&L.

### A6. Cose che funzionano e non vanno toccate

- Ancoraggio mobile: la diagnosi del 21/08 (grid fisso = scommessa contro il
  trend) resta corretta; la correzione è l'isteresi, non tornare al fisso.
- Netting come stato: niente file da riconciliare, un ordine per run.
- Soglie di profitto con pausa e decisione dell'utente: buon disegno.
- Numeri sempre dal broker, mai dal DB.
- Stop di catastrofe sulla posizione: fail-closed corretto.

## Parte B. Comunicazione con l'utente

### B1. Cosa arriva oggi su Telegram

| messaggio | quando | problema principale |
|---|---|---|
| Riepilogo orario | ogni ora, 24/7 | gergo, tre P&L diversi, arriva a mercati chiusi con "0 mosse" |
| Avvisi soglie (+5/+10, -3/-6) | a evento | corretti, ma senza azione da compiere |
| Stop perdita / kill | a evento | ha mostrato "-976,83 € da 976,83 € a 0,00 €" senza sanity check |
| Health VM | ogni giorno 09:10 | RAM, disco, vnstat: tecnico, rumore per chi non è sviluppatore |
| Settimanale | domenica 18:00 | buono nella struttura, gergo ("resa oggettiva", "equity", "flottante") |
| `/stat`, `/statN`, `/conti` | a richiesta | utili, nomi non intuitivi, nessun `/aiuto` |
| `/status`, `/posizioni` | a richiesta | comandi della v1 spenta: parlano di scan e segnali che non esistono più |
| Riparti / ferma | solo da riga di comando sulla VM | l'utente decide ma non ha il bottone |

Il riepilogo di stasera, così com'è:

```
🟢 REALE  50.63€
   oggi: +0.79€ di equity | 18 mosse chiuse -1.39€ | costi +0.01€
   dall'avvio (21/08): -0.65€
   ultima ora: 0 mosse, +0.00€
   aperte: J225 +0.1 (-0.52€), NL25 +0.01 (+0.00€)
   flottante inventario: -0.52€ (GIA' dentro l'equity qui sopra; scorta del grid, normale che sia sotto)
```

Per un lettore non esperto: semaforo verde con "-1,39 €" nella stessa riga;
"J225 +0.1" non significa nulla; "flottante inventario" è una nota a piè di
pagina infilata nel messaggio; "e'" e "GIA'" al posto di "è" e "già".

### B2. Principi per la riscrittura

1. Una domanda per riga, una sola cifra per riga: quanto ho, come è andata
   oggi, come va da quando è partito.
2. Parole di tutti i giorni: "conto" non "equity", "operazioni" non "mosse",
   "guadagno/perdita non ancora incassata" non "flottante", "conto di prova"
   non "demo".
3. Il dettaglio si chiede, non si riceve: posizioni, movimenti e costi a
   richiesta.
4. Ogni stato anomalo dice perché e cosa fare, con il comando pronto.
5. Cadenza: due messaggi al giorno nei giorni di mercato (mattina e sera),
   uno la domenica sera con la settimana, silenzio di notte e nel weekend.
   Gli eventi (soglie, stop, errori) arrivano subito. Il riepilogo orario resta
   disponibile con un comando.

### B3. Mockup

Messaggio serale (22:30, giorni feriali):

```
🌙 Chiusura di giornata · giovedì 3 settembre

💶 Conto reale: 50,63 €
   oggi: +0,79 €  🟢
   da quando è partito (21 ago): -0,65 €

🧪 Conto di prova: 976,53 €  ⏸ FERMO
   oggi: +2,68 €
   da quando è partito: -0,30 €
   Fermo dalle 19:07 per uno stop di perdita.
   Per farlo ripartire: /riparti prova

Posizioni aperte: 2 sul reale, 2 sul conto di prova (/posizioni)
Costi di oggi: 0,01 €
```

Posizioni (a richiesta, `/posizioni`):

```
📌 Posizioni aperte

Conto reale
• Nikkei (J225): comprato 1 gradino, vale 41 €, per ora -0,51 €
• Olanda (NL25): comprato 1 gradino, vale 13 €, per ora 0,00 €
Il grid tiene queste posizioni come scorta: è normale che siano
in leggera perdita finché il prezzo non torna al livello di vendita.

Conto di prova
• ...
```

Stop di perdita (rispetto a oggi: numeri plausibili o niente messaggio):

```
🛑 Stop di perdita sul conto di prova
Il conto è sceso da 976,83 € a 952,10 € (-24,73 €).
Ho chiuso 1 posizione e fermato tutti i grid del conto di prova.
Per ripartire da 952,10 €: /riparti prova
```

Lettura fallita (nuovo, al posto di un finto stop):

```
⚠️ Non riesco a leggere il conto di prova (il broker non risponde).
Non tocco nulla, riprovo al prossimo giro. Ti avviso solo se dura più di un'ora.
```

Settimanale (domenica sera):

```
📅 La settimana del conto reale
Conto: 49,79 €, questa settimana -1,55 € (-3,0 %)
Da cosa viene:
• operazioni chiuse: +1,71 €
• costi notturni del broker: -0,18 €
• dividendi: +0,06 €
• posizioni ancora aperte: -1,58 €
A questo ritmo tenere le posizioni costa circa 9 € l'anno, il 19 % del conto.
```

Mattina (08:00, giorni feriali), breve:

```
☀️ Buongiorno · venerdì 4 settembre
Conto reale 50,63 €, conto di prova 976,53 € (fermo).
Sistema: ✅ tutto regolare.
```

### B4. Comandi

| nuovo | cosa fa | sostituisce |
|---|---|---|
| `/stato` | il riepilogo come quello serale, adesso | `/stat`, `/conti`, `/grid` |
| `/posizioni` | posizioni aperte spiegate | `/posizioni` v1 (da rimuovere) |
| `/oggi` | le operazioni chiuse oggi con importo | `/statN` |
| `/riparti reale` · `/riparti prova` | sblocca dopo pausa, con conferma a bottone | solo CLI |
| `/ferma reale` · `/ferma prova` | chiude tutto e ferma, con conferma a bottone | solo CLI |
| `/aiuto` | elenco comandi in due righe ciascuno | manca |
| `/status` v1 | rimuovere: descrive lo scanner spento | |

I comandi che muovono denaro (`/riparti`, `/ferma`) vanno con bottone di
conferma e vale il gate già in memoria: mai denaro reale senza stop-loss.

### B5. Health check

Silenzio quando è tutto regolare, con una riga nel messaggio del mattino.
Messaggio dedicato solo su warning o critico, scritto in italiano piano
("la VM ha poco spazio su disco: 92 % usato").

## Parte C. Ordine di intervento

| # | intervento | tipo | sforzo | dipendenze |
|---|---|---|---|---|
| 1 | Guard su lettura conto vuota (A1), messaggio di lettura fallita | fix | 1 ora | nessuna |
| 2 | Sblocco del demo con `--riparti` | operativo | minuti | 1, decisione utente |
| 3 | Backtest a 10/15 minuti con logica esatta del job (A3) | ricerca | mezza giornata | nessuna |
| 4 | Isteresi + EMA su barre chiuse (A2), se il punto 3 conferma | strategia | 2 ore + deploy | 3 |
| 5 | Riscrittura messaggi e cadenza (B2, B3) | comunicazione | mezza giornata | nessuna |
| 6 | Comandi `/riparti`, `/ferma`, `/aiuto`, rimozione v1 (B4) | comunicazione | 2-3 ore | 5 |
| 7 | Health check silenzioso (B5) | comunicazione | 30 minuti | 5 |
| 8 | Esclusione strumenti con swap sproporzionato (A4), cap esposizione totale (A5) | strategia | 1 ora | 3 |

I punti 1, 5, 6 e 7 non cambiano la strategia e si possono fare subito. I
punti 3 e 4 sono l'unica strada per far diventare il grid quello che il disegno
promette: oggi il conto misura zero, e il "dall'avvio" sui due conti non può
dare un verdetto su una meccanica che non incassa il passo.

## Parte D. Esito del backtest intraday e cosa è stato fatto (sera del 3/9)

Il punto A3 è stato eseguito: `jobs/grid_intraday_test.py` replica la funzione
pura del job su barre a 15 minuti (180 giorni, 8 strumenti, spread reale di
ogni barra, overnight 0,03 %/notte stimato dai SWAP), con finestre mobili di
30 giorni ed euro alla taglia reale (1 unità = min size, max 2 unità).

Calibrazione sui 13 giorni reali (22/08-03/09): la simulazione della logica
attuale dà 3,7 operazioni al giorno per strumento contro 4,7 reali (il cron è a
10 minuti, la barra a 15), -0,12 € di risultato contro -0,53 € reali (trade +
posizioni aperte), 0,39 € di overnight contro 0,40 € addebitati. Riproduce il
conto, quindi il suo verdetto è credibile.

| configurazione | finestre | % positive | media € / 30 gg | peggiore | op./giorno | € per giro |
|---|---|---|---|---|---|---|
| A attuale (floor, EMA con barra corrente, 3 %) | 120 | 68 | +0,39 | -3,67 | 1,83 | +0,03 |
| G floor, EMA su barre chiuse, 3 % | 120 | 67 | +0,39 | -7,48 | 2,17 | +0,03 |
| C isteresi, EMA chiusa, 3 % | 120 | 48 | +0,35 | -3,60 | 0,11 | +0,33 |
| D isteresi, EMA chiusa, 2 % | 120 | 57 | +0,38 | -5,73 | 0,26 | +0,24 |
| E isteresi, EMA chiusa, 1,5 % | 120 | 59 | +0,14 | -6,97 | 0,44 | +0,15 |
| F isteresi, EMA chiusa, 1 % | 120 | 55 | -0,15 | -7,58 | 0,76 | +0,10 |

**Verdetto: l'isteresi NON è confermata.** La diagnosi meccanica di A2 resta
vera (i giri sono da pochi centesimi, il passo del 3 % non viene mai incassato
per intero), ma la conclusione implicita "con giri pieni si guadagna di più" è
falsa: la logica attuale è un mean-reversion ad alta frequenza attorno alla
EMA5 che guadagna poco e spesso, +0,39 € al mese alla taglia reale (circa il
9 % annuo su 50 €, ipotizzando che i prossimi mesi somiglino agli ultimi sei),
con il 68 % delle finestre positive e il caso peggiore a -3,67 €. L'isteresi
riduce le operazioni di venti volte e lascia il risultato in balìa del
mark-to-market. La stima "82 % di finestre positive" del backtest giornaliero
era ottimistica; la cifra corretta a 30 giorni è 68 %.

Conseguenza: la strategia in produzione NON cambia. L'isteresi e l'ancoraggio
su barre chiuse sono implementati e disponibili con `G2_ISTERESI=true` e
`G2_EMA_CHIUSA=true`, spenti di default. Il quinto risultato negativo del
progetto su una leva di selezione/ingresso, e la conferma che il grid è
l'unica meccanica con valore atteso positivo misurato.

Fatto in questa sessione (commit locale, poi deploy):

- A1: `leggi_equity` in `src/grid_control.py` (None se il broker non risponde
  o risponde vuoto), il job non decide nulla in quel caso; sanity check sulla
  perdita (oltre metà della base in un run = lettura sbagliata); avviso
  Telegram deduplicato una volta l'ora.
- A3: backtest intraday riusabile (`jobs/grid_intraday_test.py`, cache in
  `data/cache/`).
- B: `src/grid_report.py` riscritto (stato, mattina, sera, posizioni, oggi,
  aiuto); cadenza 08:00 e 22:30 nei giorni feriali al posto dell'orario;
  comandi `/stato /posizioni /oggi /ferma /riparti /aiuto` con bottone di
  conferma per quelli che muovono denaro; rimossi `/status` e `/posizioni`
  della v1; health check silenzioso quando è tutto regolare; messaggio
  settimanale riscritto; tutti gli avvisi del job in italiano piano con il
  comando per ripartire.
- Sblocco del demo: rimosso il blocco errato mantenendo la base del 21/08
  (non era una decisione, era un bug).

Non fatto, per scelta: A4 (esclusione HK50/J225 per lo swap: nel backtest
J225 è tra i migliori, HK50 il peggiore ma di poco; da rivalutare con più
settimane) e A5 (cap sull'esposizione totale: con max 2 unità per profilo il
tetto è già ~430 €, lo lascio come pendenza).
