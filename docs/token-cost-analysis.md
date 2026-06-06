# Diagnosi costo token Anthropic — breakdown

Data: 2026-06-06. Periodo: ultimi 14 giorni (dal 2026-05-23). Fonte:
tabella `llm_usage` (810 chiamate) + `scanner_runs`. Sola diagnosi,
nessuna modifica.

## Totale

**810 chiamate, $7.47 in 14 giorni = $0.533/giorno (~€0.49/giorno).**
Coerente con la stima ~€0.5/giorno.

## Breakdown per tipo di chiamata

| caller | n | n/gg | costo 14gg | % costo | $/gg | in/call | out/call | cache read/call | modello |
|--------|---|------|-----------|---------|------|---------|----------|-----------------|---------|
| **scanner** (scoring + thesis) | 218 | 15.6 | $6.47 | **86.7%** | $0.462 | 2272 | 1241 | 3653 | Sonnet 4-6 |
| **monitor** (HOLD/CLOSE) | 577 | 41.2 | $0.90 | 12.0% | $0.064 | 893 | 131 | 0 | Haiku 4.5 |
| events (estrazione macro) | 15 | 1.1 | $0.10 | 1.3% | $0.007 | 4515 | 437 | 0 | Haiku 4.5 |

Osservazioni chiave:
- **Lo scanner è l'86.7% del costo.** Gira su Sonnet (input $3/M, output
  $15/M), 15.6 volte/giorno. Il prompt caching 1h è già attivo (3653
  token/call serviti da cache): il system prompt è già ottimizzato, la
  parte non cacheabile (2272 in/call) sono i dati dinamici dei candidati.
- **Il monitor è solo il 12%**, già su Haiku, output minuscolo (131
  token/call). Il suo system prompt è ~222 token, **sotto la soglia minima
  di prompt caching di Haiku (~2048 token)**: non è cacheabile, lì non c'è
  leva. (E comunque è fuori perimetro per vincolo, vedi sotto.)

## Dove va davvero il costo dello scanner: gli scan "a vuoto"

**91% delle run finisce in `no_setup`** (196/215). Incrociando ogni
chiamata Sonnet con l'esito della run:

| esito run | n chiamate | out/call | in/call | costo | % costo scanner |
|-----------|-----------|----------|---------|-------|-----------------|
| **no_setup** | 196 | 1224 | 2332 | **$5.76** | **89%** |
| signal_sent | 17 | 1459 | 1917 | $0.58 | 9% |
| slots_full | 2 | 1525 | 1794 | $0.09 | 1% |

**Il numero che conta: $5.76 in 14 giorni ($0.41/giorno) è Sonnet che
gira su scan che concludono "nessun setup". È il 77% della spesa totale.**
E su quelle run l'output resta ~1224 token: il modello scrive comunque un
ragionamento verboso (a $15/M) per poi dire "niente da fare". L'output è
la singola voce più cara (1224 tok × $15/M ≈ 60% del costo per chiamata).

### Distribuzione oraria (per la leva cadenza)

Ore UTC con 0 signal in 14 giorni: **05, 06, 10, 11, 13, 17, 18** (~7 ore
× ~14 scan = ~98 scan a vuoto). Ore produttive: 09, 12, 14, 19 (3-4 signal
ciascuna), poi 07, 08, 15, 16, 20 (1-2). Cautela: 14 giorni sono pochi e
alcune "ore morte" (h13 pre-apertura USA, h17-18 pomeriggio USA) non sono
strutturalmente vuote.

## Leve di riduzione (ordinate per rapporto risparmio/rischio)

### Leva 1 — Tagliare l'output dello scanner sulle run a vuoto (MIGLIOR RAPPORTO)

L'output Sonnet ($15/M) è la voce più cara, e ~1224 token/call vengono
prodotti anche quando l'esito è `no_setup` (output poi scartato, nessun
signal). Far restituire allo scanner prima un verdetto compatto (score +
motivo breve) e generare la thesis completa **solo quando un candidato
supera la soglia** azzera l'output costoso sul 91% dei casi.
- **Risparmio stimato**: l'output no_setup è ~240k token (196×1224) ×
  $15/M = $3.6/14gg. Tagliandone il 70-80% → **~$0.18-0.21/giorno (~35-40%
  del totale)**.
- **Rischio: BASSO.** La prosa della thesis su una run no_setup è già
  scartata; accorciarla non cambia la decisione di scoring. Va solo
  preservato il ragionamento che determina lo score, tagliando
  l'elaborazione discorsiva. Resta su Sonnet, qualità di selezione intatta.

### Leva 2 — Scoring a due stadi: screen Haiku → Sonnet solo sui promettenti (RISPARMIO MAGGIORE, rischio medio)

Il 91% delle run è no_setup già dopo il pre-filtro deterministico. Un
primo passaggio su Haiku (1/3 del costo) può scartare i candidati
chiaramente non validi, chiamando Sonnet solo sul ~10-15% che può
qualificarsi.
- **Risparmio stimato**: scanner da $6.47 a ~$1.8/14gg → **~$0.33/giorno
  (~62% del totale)**.
- **Rischio: MEDIO.** Falsi negativi: lo screen Haiku potrebbe scartare un
  candidato che Sonnet avrebbe preso. **Da validare prima**: replay dei 17
  `signal_sent` storici per misurare quanti sopravviverebbero allo screen
  Haiku (recall). Solo se la recall è alta si procede.

### Leva 3 — Ridurre la cadenza nelle ore storicamente a vuoto (modesto, basso-medio rischio)

Le ~7 ore UTC senza alcun signal in 14 giorni valgono ~$2.6/14gg.
Dimezzarne la frequenza (es. scan ogni 2h invece di 1h in quelle fasce)
risparmia proporzionalmente.
- **Risparmio stimato**: ~$0.10-0.15/giorno.
- **Rischio: BASSO-MEDIO.** Perdita di copertura: 14 giorni sono un
  campione piccolo, una fascia "morta" ora potrebbe produrre setup in
  altri regimi. Meglio ridurre la frequenza che eliminare le fasce.

## Fuori perimetro: il monitor

Per **vincolo esplicito** il monitor LLM (rete di sicurezza sui trade
aperti) non si tocca. Ed è anche poco rilevante per il costo: 12% del
totale, già su Haiku, output di 131 token/call, system prompt sotto la
soglia di caching. Qualunque ottimizzazione lì avrebbe risparmio
trascurabile e metterebbe a rischio la protezione: **lasciare invariato**.

## Sintesi

Tre centesimi su quattro vanno a Sonnet che produce output verboso su scan
che decidono "nessun setup". Le leve a miglior rapporto sono sul **lato
output e sul filtraggio pre-Sonnet dello scanner**, non sul monitor:
1. Leva 1 (output trim): ~35-40% di risparmio, rischio basso. Prima scelta.
2. Leva 2 (Haiku screen): ~60% di risparmio, ma serve validare la recall.
3. Leva 3 (cadenza): ~20-30%, facile, da fare con cautela sulla copertura.
Leva 1 e Leva 3 sono combinabili e a basso rischio; la Leva 2 è la più
potente ma richiede una validazione prima di toccare la selezione.
