# TradeAlert, la storia completa

*Aggiornato al 2026-09-12. Sostituisce `riepilogo-completo.md`, fermo al 2026-07-26.*

Documento di racconto, non di specifica. Serve a chi riapre questa repo fra sei mesi
e vuole capire cosa è stato costruito, cosa è stato buttato e perché, senza rileggere
260 commit e cinquanta documenti di sprint.

---

## 1. In due minuti

Il progetto nasce il 17 aprile 2026 come sistema di swing trading assistito da un
modello linguistico su CFD Capital.com. Cinque mesi dopo, di quel sistema non è
rimasto quasi nulla: non perché sia stato abbandonato, ma perché è stato misurato.

La linea in una frase: **da un sistema che provava a prevedere la direzione del
mercato con l'IA, a un sistema che ha accettato di non saperla prevedere, ha provato
a vivere di gestione, ha scoperto che il campo da gioco è troppo stretto per i costi
del venue, e ora prova l'unica cosa che non è una previsione: incassare un premio
assicurativo.**

Nessuna delle versioni ha mai dimostrato un vantaggio stabile sul mercato. Il valore
del progetto, allo stato, è il metodo: ogni ipotesi viene pre-registrata prima di
vedere i numeri, e quasi tutte sono state dichiarate morte.

---

## 2. Le cinque fasi

### Fase 1 — MVP a segnali (17-30 aprile)

Scanner ogni ora su un paniere ristretto, feature tecniche su candele 4H, ranking dei
setup affidato a Claude Haiku, guardrail deterministici post-LLM, messaggio Telegram
con bottoni e conferma manuale, apertura su Capital, monitor delle posizioni ogni 30
minuti, trailing ogni 5 minuti, storico su Supabase. Infrastruttura su VM Oracle free
tier, migrata da GitHub Actions per latenza e affidabilità.

In undici giorni, 64 commit. A fine aprile due review indipendenti (una tecnica, una
finanziaria) hanno messo per iscritto i problemi strutturali: race condition fra
trailing, monitor e listener; nessun test automatico; e soprattutto il mismatch fra il
prompt dell'LLM e le feature realmente passate, che faceva confabulare al modello
livelli e medie mobili che non aveva mai visto. Quei due documenti sono
`review-dev.md` e `review-finance.md`, caricati con questo commit.

### Fase 2 — Gli sprint di falsificazione (maggio-luglio)

Dal secondo sprint in poi il progetto ha adottato un rituale: pre-registrare l'ipotesi
e il criterio di successo in un documento, poi eseguire il test, poi scrivere il
verdetto anche quando è negativo. Ne sono usciti sedici documenti di sprint e una
sequenza di conclusioni quasi tutte negative, riassunte al capitolo 3.

Due risultati hanno chiuso la fase. Il primo: lo score dell'LLM in ingresso ha
correlazione −0,02 con l'esito dei trade, cioè non predice niente; sostituito da una
regola deterministica gratuita, il costo LLM è crollato dell'83 % a parità di
risultato. Il secondo, più duro: scomponendo il presunto vantaggio del paniere indici
(+0,052R), 0,036R erano semplice esposizione lunga su un mercato rialzista e 0,016R
restavano dentro il rumore. Non era abilità, era beta.

### Fase 3 — Gestire l'esposizione invece di sceglierla (luglio-agosto)

Accettato che la direzione non è prevedibile, sono nati due sistemi che non provano a
prevederla.

**v2, exposure block controller**: un solo strumento, solo al rialzo, e l'unica
decisione è quanti blocchi di esposizione tenere in funzione della volatilità
misurata. Andato full-live automatico il 26 luglio. Il 1° agosto, diagnosticato uno
stallo strutturale (il conto era troppo piccolo perché il controller potesse mai
aprire), è stato reso aggressivo con micro-blocchi da 5 €.

**TradeSpinner**: non prevede, misura. Per ogni posizione possibile calcola se la
crescita composta attesa, premio meno costi, è positiva alla taglia realmente
eseguibile. Fase demo dal 28 luglio, con gate fail-closed che vieta il denaro reale
senza stop-loss obbligatorio.

Il 17 agosto lo spinner ha trovato un bug che ha cambiato le conclusioni di tutto il
filone: il costo di finanziamento overnight era sottostimato di 3,65 volte (un tasso
giornaliero letto come annuo). Con il costo vero, **il premio azionario netto su CFD è
zero**: comprare e tenere, su questo venue, non capitalizza. Insieme alla misura del
14 agosto (la staticità del motore non era colpa della calibrazione ma del venue), ha
chiuso la strada del carry lungo.

### Fase 4 — Il grid bidirezionale (18-31 agosto)

Il 18 agosto, per la prima volta, una famiglia di strategie ha dato un segnale
positivo: il grid su BTCUSD, positivo in 12 finestre mobili su 15, con 0,75-1,3 trade
al giorno, senza prevedere la direzione. Il 19 agosto è diventato **grid2
bidirezionale**: il conto Capital è in netting, quindi invece di due grid opposte c'è
una sola posizione netta che oscilla fra long e short, su strumenti scelti per spread
basso (indici e oro, non crypto minori).

Il 19 agosto v1, v2 e spinner sono stati spenti. Da quel momento il grid2 è l'unico
sistema vivo, su sei profili sul conto reale e otto sul demo. Al checkpoint del 27
agosto entrambi i conti erano positivi dall'avvio: reale +0,53 €, demo +2,23 €.

### Fase 5 — Controanalisi, ClaudeTrade e la vendita di assicurazione (settembre)

Il **3 settembre** una controanalisi generale ha trovato un bug critico attivo: una
lettura del saldo andata in timeout veniva interpretata come equity pari a zero, e
faceva scattare uno stop di perdita inesistente da −976 € che aveva già fermato tutti
i grid demo. Fail-open diventato fail-closed. Nella stessa analisi è emerso che il
grid non incassava affatto il passo dichiarato del 3 %: l'80 % dei fill distava meno
dello 0,15 % dal precedente, cioè chattering sul confine del gradino. Da lì la griglia
è passata da 10 a 30 minuti, e i messaggi Telegram sono stati riscritti in italiano
piano con comandi espliciti.

Il **6-7 settembre** è nato **ClaudeTrade**: il conto di prova viene letto come se
avesse 200 €, con resoconto serale su Telegram e una pagina pubblica rigenerata ogni
mezz'ora su https://claudetrade.eu, diario incluso. È l'esercizio in chiaro: capitale
dichiarato, risultati pubblicati, nessuna promessa.

Il **7 settembre**, misurando il campo da gioco su tutto l'universo negoziabile, è
arrivata la diagnosi più utile di tutte: sugli indici il rapporto fra movimento
tipico giornaliero e spread è circa 8 a 1, troppo sottile perché il segno del
risultato dipenda dalle decisioni invece che dal caso. Un sistema che entra ed esce
spesso, lì dentro, produce rumore.

Nello stesso giorno, dopo diciassette ipotesi cadute, la prima che sopravvive a tutte
le prove: **il VIX predice i rendimenti futuri degli indici**. È l'unica cosa provata
che non usa soltanto il prezzo dello strumento su cui si opera. E la sua applicazione
pratica non è una previsione ma un premio: gli strumenti che comprano protezione
dalla volatilità perdono valore per costruzione (UVXY −79,8 % l'anno su quindici
anni). Stare dall'altra parte significa incassare quel decadimento. Dal 7 settembre
l'esecutore gira sul solo conto di prova, alle 16:00 nei giorni feriali, con tetto di
esposizione e stop di emergenza.

La natura del rendimento è dichiarata senza ammorbidirla: si guadagna poco quasi
sempre e si perde molto raramente. Il 5 febbraio 2018 UVXY è salito del 66 % in una
seduta, e con l'esposizione prevista sarebbe stata una perdita del 10 % del capitale
in un giorno.

---

## 3. Il cimitero delle ipotesi

Tutte pre-registrate prima di vedere i numeri, tutte con documento in `docs/`.

| Ipotesi | Esito | Quando |
|---|---|---|
| Lo score LLM seleziona i setup migliori | NEGATIVO (corr −0,02) | luglio |
| Il tempismo di ingresso conta (chasing) | NEGATIVO | 09/06 |
| Un filtro di regime migliora la selezione | NEGATIVO, taglia i TP grandi | 10/06 |
| Il trailing più stretto è meglio | POSITIVO poi ARTEFATTO di granularità | 06→07 |
| Uscita a target fisso in euro | NEGATIVO, taglia corti i winner | 28/06 |
| Gli asiatici (HK50, J225) hanno rischio gap specifico | NON CONFERMATO, peggio è Brent | 30/06 |
| Tenere nel weekend ha un edge | NO, solo rischio di coda | luglio |
| Scansionare più spesso aiuta (in ingresso e in uscita) | NEGATIVO su entrambi | 22/07 |
| Un motore deterministico batte l'LLM | FALSIFICATO da walk-forward | 02/07 |
| L'edge del paniere indici è abilità | NO, è beta lungo | 26/07 |
| Il buy&hold capitalizza su CFD | NO, col costo vero il premio netto è zero | 17/08 |
| Il momentum fra strumenti | ARCHIVIATO, il costo delle due gambe lo schiaccia | 07/09 |
| Esiste un campo da gioco migliore per il grid | NO, movimento/costo resta ~8:1 | 07/09 |
| Il VIX predice i rendimenti | **POSITIVO**, unico sopravvissuto | 07/09 |

---

## 4. I bug che hanno cambiato le conclusioni, non solo il codice

Tre errori hanno prodotto, per un periodo, numeri sbagliati su cui erano state prese
decisioni. Vale la pena ricordarli perché sono la ragione per cui oggi ogni misura
viene riletta dal broker e non dal database.

1. **Finanziamento sottostimato 3,65 volte** (fix `3c1f405`, 17 agosto). Un tasso
   giornaliero trattato come annuo. Col costo vero, un'intera famiglia di strategie
   passa da marginalmente positiva a strutturalmente negativa.
2. **Equity contata due volte** (fix `5a5805b`, 1 settembre). Il campo `balance` di
   Capital è già l'equity, comprensiva del flottante: sommarci il P&L aperto gonfiava
   il conto.
3. **P&L in euro col segno invertito nel database**. Il conto reale letto dal broker
   dava +28,32 € di trading mentre il database diceva −5,82 €. Finché non è chiuso,
   i report in euro che nascono dal DB non sono affidabili, e il conto reale si legge
   solo dalla VM.

A questi si aggiunge il sizing che usava la leva di categoria invece di quella reale
per strumento (fix `555adc3`, 21 luglio): i setup "da 40 €" bloccavano circa il doppio
di margine.

---

## 5. Cosa gira adesso (12 settembre 2026)

Tutto sulla VM Oracle, cron classico, nessuna chiamata LLM in produzione.

- **grid2 bidirezionale**, ogni 30 minuti, sei profili sul conto reale (NAS, JP, ORO,
  NL, HK, US30) e otto sul demo. È l'unico sistema che tocca denaro reale.
- **ClaudeTrade**, l'esercizio a capitale dichiarato da 200 € sul conto di prova, con
  resoconto Telegram alle 22:30 nei feriali e pagina pubblica su claudetrade.eu
  rigenerata ogni mezz'ora.
- **Vendita di assicurazione sulla volatilità**, alle 16:00 nei feriali, solo conto di
  prova, con tetto di esposizione e stop di emergenza con pausa obbligatoria.
- **Riconciliazione oraria** con il broker, health check giornaliero alle 9:10,
  verità di conto settimanale la domenica alle 18:00, riepilogo orario e buongiorno
  alle 8:00 su Telegram.

Spenti e non più manutenuti: v1 a segnali, v2 exposure controller, TradeSpinner.
Restano nel repository come storia e come codice riutilizzabile.

---

## 6. I documenti caricati con questo commit

Erano rimasti fuori dal versionamento per mesi. Sono materiale d'epoca, non
aggiornato: vanno letti con la data in testa.

- `overview.md` — la fotografia del sistema al 27 aprile 2026, com'era pensato in
  origine. Utile per capire da dove si è partiti.
- `review-dev.md` — review tecnica del 25 aprile: race condition, assenza di test,
  chiavi Supabase nei workflow, costi LLM.
- `review-finance.md` — review trading del 25 aprile: il mismatch prompt/feature,
  l'esposizione weekend senza protezione dai gap, l'assenza di cap di drawdown. È il
  documento che ha anticipato metà degli sprint successivi.
- `setups.txt` — export completo dei primi 48 setup con tesi, esiti ed eventi di
  trailing. Il dataset grezzo su cui è stata costruita gran parte delle analisi.
- `sprint5-weekend-gap.md` — la pre-registrazione del test sui gap asiatici, poi
  falsificato.

---

## 7. Cosa resta aperto

- Il P&L in euro nel database ha ancora il segno invertito: va sistemato prima di
  fidarsi di qualunque report in euro che non venga letto dal broker.
- Il grid2 sui due conti non ha ancora un verdetto: l'unico dato è che dall'avvio del
  21 agosto entrambi i conti sono positivi, su numeri troppo piccoli per concludere.
- La vendita di assicurazione è in prova da pochi giorni e non ha mai visto uno shock
  di volatilità. Il primo vero test sarà quello, e sarà brutto per costruzione.
- Nessun test automatico in repository, tuttora. È il debito tecnico più vecchio,
  segnalato ad aprile e mai saldato.
