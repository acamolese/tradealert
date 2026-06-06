# Analisi stabilità dello scoring scanner

Data: 2026-06-06. Sola analisi, nessuna modifica. Indaga due comportamenti
dello scoring emersi durante il test costo token: (1) lo score dipende
dalla composizione dell'universo, (2) jitter elevato a temperatura 1.0.
Possibile collegamento col finding Sprint 3.5 (lo score non discrimina i
rapid loss).

## Come funziona lo scoring (verificato sul codice)

`src/llm_analyzer.py::rank_setups`: **una sola chiamata Sonnet** valuta
tutto l'universo e restituisce i top-3 setup con score + thesis insieme.
Il prompt è **ibrido**:
- chiede un **ranking relativo** ("Produci un ranking dei top 3 setup");
- ma con **àncore assolute** ("score 0-10, 8+ eccellenti, 6-7 buoni, sotto
  6 mediocri"; "se nessun asset ha setup decente, tutti score sotto 6").

Questa tensione (ranking + scala assoluta nella stessa risposta) è la
radice del Finding 1: il punteggio "assoluto" assorbe il contesto
competitivo dell'universo. Inoltre lo score è generato nella stessa
chiamata della thesis, che gira a **temperatura 1.0** (default: `rank_setups`
non passa `temperature`), radice del Finding 2.

## Finding 1 — Score relativo alla composizione dell'universo

Test: Gold con feature FISSE, variando solo i co-asset nell'universo, temp 0,
3 run per composizione.

| universo | score Gold (×3) |
|----------|-----------------|
| Gold + Brent | 6.5, 6.5, 6.5 |
| Gold + Bitcoin | 7.0, 7.0, 7.0 |
| Gold + Nasdaq | 7.0, 7.0, 7.0 |
| Gold + US500 | 7.0, 7.0, 7.0 |
| Gold + Brent + Nasdaq | 6.5, 6.5, 6.5 |

**Swing: Δ 0.5** (6.5 ↔ 7.0), perfettamente stabile entro composizione
(temp 0), quindi è un effetto reale della composizione, non rumore. La
presenza di **Brent** (setup forte) spinge Gold a 6.5; senza Brent, Gold
sale a 7.0.

Implicazione, ed è il punto importante: **Δ 0.5 attraversa la soglia 7**.
Lo stesso identico setup di Gold viene aperto (7.0) o scartato (6.5) a
seconda di cosa c'è nell'universo quell'ora. Il regime di selezione non è
"qualità assoluta sopra una soglia" ma un **ibrido che pende verso il
meglio del gruppo**: un campo debole promuove un setto borderline, un
competitor forte lo declassa.

(Nota: l'impressione iniziale di Δ 1.0 (6.5 vs 7.5) era gonfiata dal jitter
a temp 1.0; la misura pulita a temp 0 è Δ 0.5.)

## Finding 2 — Jitter a temperatura 1.0

Test: universo FISSO (Gold + Nasdaq + Bitcoin), stesso input, 12 run a temp
1.0 (produzione) e 5 run a temp 0 (pavimento del rumore).

| asset | temp 1.0: media | sd | range | temp 0: range |
|-------|-----------------|----|-------|---------------|
| Gold | 7.17 | 0.20 | 7.0-7.5 | 7.0-7.5 |
| Nasdaq 100 | 6.09 | 0.52 | **4.5-6.5** | 6.0-6.5 |
| Bitcoin | 4.62 | 0.41 | 4.5-6.0 | 4.5-4.5 |

Conferme:
- Il jitter è **asset-dipendente**: un setup netto (Gold qui) è stabile
  (sd 0.20), uno ambiguo/borderline (Nasdaq) oscilla fino a **range 2.0**
  (4.5-6.5) sullo stesso identico input.
- È **guidato dalla temperatura**: a temp 0 i range si stringono molto
  (Bitcoin addirittura piatto a 4.5). Quindi la varianza è in gran parte
  artefatto del temp 1.0, non rumore irriducibile.
- **Perché temp 1.0**: score e thesis sono nella stessa chiamata, e la
  temperatura alta serve alla varietà della PROSA; lo score la eredita
  senza motivo, ereditando varianza dove servirebbe determinismo.

### Finding 2b — Quasi tutti i signal aperti sono borderline

Distribuzione degli score dei signal sopra soglia (score ≥ 7, gli unici che
aprono), su 101 signal storici:

| score | n |
|-------|---|
| 7.0 | 43 |
| 7.2 | 31 |
| 7.5 | 20 |
| 7.8 | 4 |
| 8.0 | 3 |

**Borderline 7.0-7.5: 94/101 = 93%.** E **43/101 (43%) stanno esattamente
a 7.0**, cioè proprio sul filo della soglia. Solo 7 signal su 101 (i 7.8 e
8.0) sono "convintamente sopra soglia".

Mettendo insieme con il Finding 2: vicino alla soglia la sd è ~0.2-0.5 (fino
a range 2.0 sugli ambigui). Un "vero 6.5" può uscire 7.0 a un tiro di dado,
e un "vero 7.0" può scendere a 6.5. Con il 93% degli open raggruppati a
7.0-7.5 e il 43% esatto a 7.0, **una quota sostanziale degli open è decisa
da rumore + composizione, non da un segnale di qualità robusto**. Non posso
contare esattamente quanti senza rigirare ogni scan storico molte volte
(vedi limiti), ma strutturalmente l'effetto è di primo ordine, non marginale.

## Collegamento con Sprint 3.5 — CONFERMATO

Sprint 3.5 (`docs/sprint3.5-entry-diagnosis.md`) aveva trovato che lo score
non discrimina i rapid loss: mediana **7.25** (rapid loss) vs **7.00**
(developed), una separazione di appena **0.25**.

Questa analisi spiega perché: la separazione utile (0.25) è **più piccola
del rumore dello score stesso**:
- jitter a temp 1.0: sd ~0.2-0.5 (≥ 0.25);
- swing da composizione: ~0.5 (≥ 0.25).

In altre parole, il segnale discriminante dello score è sommerso dalla sua
stessa varianza (temperatura) e dalla sua dipendenza dal contesto
(composizione universo). **Lo score, così com'è, è strutturalmente poco
informativo come predittore di qualità del setup**: non perché il modello
non sappia valutare, ma perché lo si legge a temperatura alta, su una scala
relativa, con una soglia che cade nel mezzo della nuvola di rumore.

## Ipotesi per Sprint 4 (NON azioni, da validare)

1. **Temperatura bassa per lo scoring** (es. temp 0-0.3). Se score e thesis
   restano in una chiamata, separarli permette temp bassa sullo score e
   temp alta solo sulla prosa. Atteso: collassa il jitter (Finding 2),
   rende la soglia 7 stabile.
2. **Valutazione assoluta per-asset** invece del ranking di gruppo, per
   togliere la dipendenza dalla composizione (Finding 1). Trade-off:
   perde il confronto relativo, da pesare.
3. **Separare score e thesis in due chiamate** (converge con Leva 1/2
   costo token): abilita 1 e 2 insieme.
4. **Rivedere la soglia o renderla a banda** (es. zona 6.5-7.5 = "incerto")
   visto che il 93% degli open ci cade dentro: la soglia secca a 7 su una
   scala rumorosa è fragile.

Se una di queste riduce il rumore, lo score potrebbe tornare a discriminare
e il finding Sprint 3.5 andrebbe rimisurato su score "puliti".

## Limiti (sample)

- Universi sintetici: feature prese dall'ultimo signal per asset, da
  timestamp diversi; gli score assoluti non sono quelli storici esatti.
- Finding 1 sondato solo su Gold come base; 3 run per composizione. Una
  misura completa varierebbe più asset-base e universi più grandi (con il
  caveat che oltre 3 asset il top-3 può non includere l'asset target).
- Finding 2 su un solo universo, 12 run. La sd è indicativa, non un
  intervallo di confidenza stretto.
- "Open da rumore" stimati strutturalmente, non contati: servirebbe
  rigirare ogni scan storico N volte (costoso) per la frazione esatta.
- Tutto da riconfermare con dati Sprint 3 / uno shadow test in produzione.
