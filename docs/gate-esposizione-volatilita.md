# Gate pre-registrato: la scala di esposizione serve?

Pre-registrato il **2026-09-12**, prima di aver visto un solo giorno di dati
della scala. Come per gli altri gate del progetto, il criterio è scritto adesso
proprio perché a posteriori sarebbe facile trovare una lettura che assolve la
complessità appena introdotta.

## Cosa è cambiato il 2026-09-12

Fino a ieri la vendita di assicurazione sulla volatilità teneva un'esposizione
fissa: 15% del capitale dichiarato, sempre, con tetto al 25% e stop a 15 €.

Da oggi la taglia è un gradino scelto su condizioni verificabili:

| Gradino | Esposizione | Quando |
|---|---|---|
| ritirata | 0% | VIX ≥ 30, oppure curva sotto 0,85 del suo anno |
| base | 15% | condizioni ordinarie, o dati mancanti |
| favorevole | 20% | curva ≥ 0,93, VIX ≤ 25, ≥ 20 giorni di vita, nessuno stop da 30 giorni, risultato cumulato ≥ 0 |
| pieno | 25% | curva ≥ 0,97, VIX ≤ 20, ≥ 40 giorni di vita, stesse condizioni di conto |

Il tetto (25%) e lo stop in euro (15 €) non sono cambiati: **il rischio massimo
autorizzato è identico a prima**. Quello che cambia è quanto spesso ci si sta
vicino, e la comparsa di un'uscita completa che prima non esisteva.

## La misura della pendenza, e perché non è ovvia

Capital quota VIX (un indice) e VIXM (un ETF sui futures a medio termine). Il
loro rapporto grezzo non è la pendenza della curva: è un prezzo diviso un
livello, e l'ETF scivola per costruzione. Nei dati settimanali Capital la
mediana di quel rapporto è 1,79 nel 2021, 0,84 nel 2025, 0,77 nel 2026. Una
soglia fissa avrebbe misurato il decadimento dell'ETF, non il mercato.

Il sistema usa quindi la **pendenza relativa**: rapporto corrente diviso la sua
mediana delle ultime 52 settimane. Sui dati disponibili separa i regimi:

- settimane di stress (VIX 24-28, marzo 2026): da 0,74 a 0,87
- settimane calme (VIX 17-18): da 0,94 a 1,02

Limite dichiarato: l'intersezione utile fra le due serie è di circa 87 settimane
e contiene **un solo episodio di stress moderato**, con VIX massimo 27,6. Non
esiste nel campione un evento tipo febbraio 2018 o marzo 2020. Le soglie sono
quindi provvisorie per costruzione, e questo gate serve anche a ritararle.

## Le tre varianti misurate in ombra

`jobs/vol_shadow.py` registra ogni giorno, senza eseguire nulla:

- **fissa**: 15% sempre, la baseline, cioè il comportamento fino al 2026-09-11
- **scala**: quello che il sistema esegue davvero
- **spinta**: stesse condizioni, gradini 20/30/40% e tetto 40%

La variante spinta esiste per rispondere senza rischiare alla domanda "conveniva
osare di più": se a fine finestra la spinta risultasse nettamente migliore, la
decisione di alzare il tetto sarebbe informata e non un atto di fede.

## Criterio di valutazione

**Quando**: al primo fra queste due condizioni, non prima.

- 90 giorni di calendario dal 2026-09-12, cioè dal **2026-12-11**
- oppure almeno 60 giorni con mercato aperto E almeno un episodio con VIX ≥ 25

**Dato**: rendimento in euro della posizione, ricostruito dai prezzi UVXY
registrati in `vol.segnale_shadow` e dalle decisioni in `vol.decisione`,
applicando a ciascuna variante la sua frazione giornaliera e i costi veri
(spread 0,74% per giro, finanziamento letto dal broker).

**La scala si tiene** se, rispetto alla baseline fissa, vale ALMENO UNA di:

1. rendimento cumulato ≥ quello della fissa **e** peggior giorno migliore di
   almeno il 20% (cioè: stessa resa, meno dolore)
2. rendimento cumulato superiore di almeno il 15% **e** peggior giorno non
   peggiore della fissa

**La scala si spegne** e si torna all'esposizione fissa se:

- il rendimento cumulato è inferiore alla fissa **e** il peggior giorno non è
  migliore, cioè la complessità non ha comprato niente
- oppure la ritirata è scattata più di 6 volte in 90 giorni senza che nei 5
  giorni successivi il VIX sia salito: significherebbe che il segnale esce per
  rumore, pagando spread ogni volta

**Verdetto sulla variante spinta**: si alza il tetto solo se la spinta batte la
scala su ENTRAMBI i criteri (rendimento e peggior giorno), il che è improbabile
per costruzione e va bene così: il tetto è l'unico numero che protegge dal caso
estremo, e va alzato solo con una prova, mai per entusiasmo.

## Cosa NON dimostrerà questo gate

Che la strategia funziona. Quella domanda è separata e ha bisogno di anni, non
di mesi: il rendimento di chi vende assicurazione si guadagna poco per volta e
si perde tutto insieme, e in 90 giorni la probabilità di vedere l'evento raro è
bassa. Qui si misura soltanto se **modulare** la taglia è meglio che tenerla
ferma, a parità di strategia.

Se in finestra capitasse un evento estremo, il gate va letto al contrario: non
conterà il rendimento cumulato, ma se la ritirata è scattata prima del danno.
Quel singolo dato varrebbe più di tutti gli altri.
