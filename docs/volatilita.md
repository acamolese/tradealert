# Vendere assicurazione sulla volatilità

*Stato al 2026-09-12. Sistema attivo sul solo conto di prova.*

## Cosa fa, in una riga

Sta corto su UVXY, uno strumento costruito per perdere valore, e incassa quel
decadimento. Non prevede la direzione di niente.

## Perché dovrebbe funzionare

UVXY rinnova ogni giorno contratti a termine sulla volatilità comprandoli più
cari di quelli che scadono. Chi lo detiene paga quella differenza, chi sta
dall'altra parte la incassa. Misurato su quindici anni con i costi veri di
Capital: UVXY −79,8% l'anno, VIXY −51%, VXX −41,3%. È il premio di chi vende
protezione: si guadagna poco quasi sempre e si perde molto raramente.

Il 5 febbraio 2018 UVXY è salito del 66% in una seduta. Quella è la forma del
rischio, e non si può eliminare: si può solo dimensionare.

## Come è fatto

| Pezzo | File | Cosa fa |
|---|---|---|
| Decisioni | `src/volatilita.py` | logica pura: gradino, taglia, stop, kill switch. Nessun I/O, tutto testabile |
| Parametri | `config/volatilita.json` | soglie e taglie, versionate. Le variabili `PAURA_*` sulla VM vincono, per correggere senza deploy |
| Segnale | `src/vol_segnale.py` | legge VIX e VIXM da Capital e normalizza la pendenza |
| Persistenza | `src/vol_store.py` | scrive decisioni, posizioni e misure in ombra su Supabase, schema `vol` |
| Orchestrazione | `src/vol_runner.py` | mette insieme i pezzi, parla col broker e con Telegram |
| Ingresso | `jobs/paura_esegui.py` | il job, alle 16:00 nei feriali |
| Misura in ombra | `jobs/vol_shadow.py` | registra le varianti senza eseguirle |
| Test | `tests/test_volatilita.py` | 48 test, `python -m unittest discover tests` |

## La scala dell'esposizione

Dal 2026-09-12 la taglia non è più fissa al 15%: è un gradino scelto su
condizioni verificabili, e ogni cambio viene registrato con le feature che lo
hanno prodotto.

- **ritirata (0%)**: VIX ≥ 30, oppure curva sotto 0,85 del suo anno. Il premio
  si spegne proprio quando il mercato si agita, e restare esposti in quel
  momento è il modo classico di perdere tutto insieme.
- **base (15%)**: condizioni ordinarie. È anche il gradino di sicurezza quando
  un dato manca: senza informazione non si sale mai, e non si chiude mai.
- **favorevole (20%)**: curva ≥ 0,93, VIX ≤ 25, almeno 20 giorni di vita del
  sistema, nessuno stop negli ultimi 30 giorni, risultato cumulato ≥ 0.
- **pieno (25%)**: curva ≥ 0,97, VIX ≤ 20, almeno 40 giorni di vita.

Il tetto resta 25% e lo stop resta 15 €: il rischio massimo autorizzato non è
cambiato rispetto alla versione a esposizione fissa. Quello che cambia è quanto
spesso ci si sta vicino.

Il prezzo di salire è scritto ogni volta nel messaggio Telegram: con 30 € di
esposizione un salto del 66% costa circa 20 €, con 50 € ne costa 33.

## La misura della curva, e l'errore che stava per passare

Capital quota VIX (indice) e VIXM (ETF sui futures a medio termine). Il rapporto
grezzo fra i due **non** è la pendenza della curva: è un prezzo diviso un
livello, e l'ETF scivola per costruzione. La mediana di quel rapporto nei dati
Capital è 1,79 nel 2021 e 0,77 nel 2026.

La prima versione di questo codice usava una soglia fissa a 1,05 per dire
"contango". Con i valori reali di oggi (0,75) il sistema sarebbe rimasto in
ritirata permanente, cioè non avrebbe mai aperto, e il log avrebbe detto "curva
invertita" in un mercato tranquillo con VIX a 18. L'errore è emerso al primo giro
in sola lettura contro il broker.

La versione corretta usa la **pendenza relativa**: rapporto corrente diviso la
sua mediana delle ultime 52 settimane. Sui dati disponibili separa i regimi
(stress 0,74-0,87, calma 0,94-1,02), e assorbe il drift dell'ETF.

Limite dichiarato: circa 87 settimane utili, un solo episodio di stress moderato
(VIX 27,6). Nessun evento estremo nel campione. Le soglie sono provvisorie e il
gate in `gate-esposizione-volatilita.md` le rivedrà.

## Le protezioni, in ordine di precedenza

1. **Conto**: gira solo se l'ambiente è il conto di prova. La verifica è una
   funzione pura (`conto_autorizzato`) proprio per poterla testare davvero.
2. **Pausa**: dopo uno stop il sistema resta fermo 5 giorni. Dopo uno strappo la
   volatilità resta alta e riaprire subito è la cosa peggiore.
3. **Kill switch**: se la posizione perde più di 15 €, si chiude tutto.
4. **Ritirata**: VIX alto o curva che si appiattisce portano l'esposizione a
   zero.
5. **Stop sul broker**: piazzato all'apertura, perché il controllo giornaliero
   non protegge dai salti notturni.
6. **Tetto**: nessun gradino può superarlo, nemmeno per un errore di
   configurazione (il loader riporta dentro i valori e lo scrive nel log).

## Cosa viene registrato

Schema `vol` su Supabase (migration `20260912150000_vol_schema.sql`):

- `vol.decisione`: una riga per ogni run, **anche quando il sistema non fa
  nulla**, con `features_at_decision` (VIX, VIXM, pendenza, percentile, stato
  del conto). Senza le decisioni di non agire non si può ricostruire per quanto
  tempo una condizione ha tenuto fermo il sistema.
- `vol.posizione`: aperture e chiusure, per riconciliare con il broker.
- `vol.segnale_shadow`: una riga al giorno con le tre varianti misurate.

La persistenza non blocca mai il trading: se Supabase non risponde, il sistema
opera lo stesso e l'errore finisce nel log. Il rovescio è dichiarato: quel run
non esisterà nelle analisi.

## Riconciliazione

Il job orario `jobs/reconcile.py` chiude anche le righe `vol.posizione` rimaste
aperte quando sul broker non c'è più niente (stop di mercato scattato di notte,
chiusura manuale dal frontend). Un fallimento lì non fa fallire il reconcile
principale.

## Cosa manca

- Un backtest della scala sui dati storici: oggi esiste solo quello della
  strategia a esposizione fissa (`jobs/vendere_paura.py`, quindici anni Yahoo).
  La scala si misura in avanti, con lo shadow, perché la serie VIXM di Capital
  non è abbastanza lunga per simularla all'indietro in modo onesto.
- Il P&L realizzato viene accumulato nello stato locale, non letto dal broker:
  è la stessa debolezza che altrove ha prodotto numeri sbagliati, e va sostituito
  con una lettura dalle transazioni.
- Nessun test tocca il broker: le funzioni di I/O sono coperte solo di riflesso.
