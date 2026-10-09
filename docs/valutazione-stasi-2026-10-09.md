# Valutazione della stasi, 9 ottobre 2026

*Domanda dell'utente: il sistema non guadagna e non perde; i dati bastano per
giudicare, e si può alzare il rischio per "prendere una direzione"?*

Fonte dei numeri: transazioni del broker lette dalla VM (`/history/transactions`,
mai il DB), equity per run dai registri `data/decisioni_*.csv`, mercati Capital
interrogati il 9/10 su entrambi i conti. Script nello scratchpad di sessione,
non versionati.

## 1. I numeri

| | Reale (grid2, 6 profili) | ClaudeTrade (demo letto a 200 €, 8 profili) |
|---|---|---|
| periodo | 21/08 → 09/10 (49 gg, 33 di mercato con registro) | 07/09 → 09/10 (33 gg) |
| equity | 51,28 → 51,18 (**−0,10 €**, −0,2%) | 979,44 → 978,38 (**−1,06 €**, −0,5% su 200) |
| realizzato | +0,08 € | −0,57 € |
| di cui operazioni | +0,92 € (273 chiusure) | +0,10 € (189 chiusure) |
| di cui finanziamento notturno | **−1,33 €** | **−1,05 €** |
| di cui dividendi | +0,49 € | +0,38 € |
| chiusure vincenti / perdenti / a zero | 189 / 51 / 33 | 132 / 43 / 14 |
| media per chiusura | +0,004 € | +0,001 € |
| lordo scambiato (somma dei valori assoluti) | 18,32 € | 17,24 € |
| escursione equity | 47,97 ÷ 52,17 | 974,96 ÷ 979,91 |
| peggior drawdown | −3,40 € | −4,95 € |
| delta giornaliero: media / deviazione | +0,005 / 0,71 € | −0,03 / 1,00 € |
| giorni positivi / negativi | 14 / 18 | 13 / 19 |

Il delta medio giornaliero è lo 0,7% della sua deviazione standard (t ≈ 0,04 sul
reale, −0,17 sul demo). Non è "quasi positivo" né "quasi negativo": è zero con
qualunque tolleranza, e con 33 giorni il più piccolo effetto rilevabile sarebbe
circa ±0,25 €/giorno. L'impressione di stasi è corretta ed è statistica, non
visiva.

Il profilo delle chiusure (79% vincenti da +0,05 € medi, 21% perdenti da −0,17 €
medi) è quello classico di chi compra i cali a taglia fissa: molti spiccioli
quando il prezzo torna, una perdita più grande quando non torna. Le due code si
compensano al centesimo.

## 2. Perché la stasi è strutturale, non sfortuna

**Il grid "bidirezionale" non è mai andato corto.** Su 14 profili e 21.300 run,
la quota di tempo in posizione corta è 0,0% ovunque. Con ancoraggio EMA5 e passo
3%, lo scostamento del prezzo dall'ancora ha mediana 0,44%, 99° percentile 2,38%,
massimo 2,74%: non arriva mai al +3% che farebbe scattare il verso corto, e non
arriva mai al −3% che farebbe scattare la seconda unità. In pratica il sistema è
"lungo di una unità minima quando il prezzo sta sotto la sua media a 5 barre,
piatto altrimenti". Il disegno a gradini da 3% non è mai entrato in funzione: è
l'ingranaggio sbagliato per l'ampiezza reale dei movimenti, come già diagnosticato
il 3/9 (chattering) e il 7/9 (movimento/costo 8:1).

| strumento | tempo lungo (reale) | cambi di posizione | spread medio |
|---|---|---|---|
| US30 | 80% | 52 | 0,004% |
| GOLD | 77% | 59 | 0,013% |
| HK50 | 75% | 29 | **0,074%** |
| NL25 | 44% | 32 | 0,009% |
| J225 | 29% | 45 | 0,016% |
| US100 | 28% | 47 | 0,006% |

Stare lunghi il 75-80% del tempo costa finanziamento: −1,33 € in sette settimane
sul reale, cioè circa −2,6% del conto, circa −19% annualizzato. È la voce più
grande del bilancio, più grande del guadagno di trading. Il carry netto
(finanziamento + dividendi) è −0,84 €; il trading lo compensa per caso.

## 3. Alzare il rischio sul grid

Moltiplicare la taglia moltiplica media e deviazione nella stessa proporzione. Con
media indistinguibile da zero il risultato è una passeggiata casuale più ampia,
non una direzione. Due vincoli concreti sul conto reale:

- **Margine**: una unità vale 40-55 € di nozionale (2-2,5 € di margine al 5%).
  Con 49 € disponibili il tetto è circa 1,7 volte la taglia attuale, non 3 o 5.
- **Stop di conto**: lo stop a −6 € con deviazione giornaliera 0,71 € ha circa il
  14% di probabilità di scattare per solo rumore in 33 giorni. A taglia tripla
  (deviazione ≈ 2,1 €/giorno) sale a circa il 62%, mentre il finanziamento
  triplica a circa −4 €/sette settimane. Si otterrebbe quasi certamente una
  direzione: verso lo stop.

**Verdetto: NEGATIVO.** Non c'è una taglia che trasformi un'attesa nulla in una
positiva. L'unico intervento sul grid che ha una giustificazione oggettiva è
tagliare un costo certo: HK50 ha spread dieci volte gli altri ed è il peggiore su
entrambi i conti (−1,60 € reale, −1,24 € demo). È un argomento di costo, non una
previsione di rendimento.

## 4. La scoperta che cambia il quadro: l'assicurazione non è mai partita

`UVXY` su Capital è **`marketModes: ["LONG_ONLY"]`**, su demo e su reale. Non si
può vendere allo scoperto. Dal 16/09 il job delle 16:00 ha mandato ogni giorno un
ordine `SELL 2`: il broker risponde 200 con un `dealReference` e poi lo rifiuta,
ma il codice non chiama `confirm_deal` e registra l'ordine come eseguito. Nel
registro transazioni del demo non esiste una sola riga UVXY; la posizione è
sempre stata zero; il messaggio Telegram "Assicurazione: partita" del 16/09
descriveva una posizione che non è mai esistita. Quattordici ordini, quattordici
rifiuti silenziosi.

Quindi l'unica ipotesi sopravvissuta alla pre-registrazione (il premio di
volatilità) non è eseguibile nella forma disegnata, e il gate del 2026-12-11 sta
misurando il nulla.

Alternativa eseguibile: **SVXY lungo** (ETF −0,5x sul VIX a breve, `LONG_ONLY`
va bene perché si compra). VXX, VIXY, SVIX non esistono su Capital. Con dati
Yahoo e costi Capital reali (finanziamento lungo 0,0222%/giorno = **8,1%/anno**,
spread 0,29%):

| periodo | CAGR lordo | netto del finanziamento | maxDD | peggior giorno |
|---|---|---|---|---|
| dal 2018-03 (leva −0,5x) | +12,3%/a | ≈ +4%/a | −62% | −21,4% |
| dal 2020-04 | +25,2%/a | ≈ +17%/a | −46% | −21,4% |
| ultimi 12 mesi | +26,4%/a | ≈ +18%/a | −23% | −6,8% |

Su ClaudeTrade (200 €), esposizione costante ribilanciata ogni mese:

| esposizione | €/anno (dal 2018) | €/anno (dal 2020) | maxDD € | peggior giorno € |
|---|---|---|---|---|
| 15% (30 €) | +2,5 | +5,2 | −18 ÷ −23 | −6 |
| 25% (50 €) | +3,8 | +8,4 | −33 ÷ −38 | −10 ÷ −11 |
| 40% (80 €) | +5,3 | +12,9 | −58 ÷ −61 | −17 ÷ −20 |

Il finanziamento lungo di Capital si mangia due terzi del premio: shortare UVXY
costava 0,56%/anno, comprare SVXY costa 8,1%. L'edge sopravvive, ma dimagrito, e
resta una scommessa sulla calma con coda brutta (il −0,5x ha perso il 46% nel
marzo 2020; il −1x che lo precedeva perse l'83% in un giorno nel 2018).

## 5. Altri difetti trovati

- `jobs/account_truth --weekly` va in crash da cinque domeniche
  (`'CapitalClient' object has no attribute 'config'`, riga 137): la "verità di
  conto settimanale" su Telegram non esiste da inizio settembre.
- La VM è a `8a9a806`; il commit `8ce0661` (scala di esposizione volatilità) è
  solo in locale, push mai avvenuto. Finché UVXY non è vendibile il deploy è
  comunque inutile.

## 6. Cosa fare, in ordine

1. **Chiudere la posizione fantasma**: far verificare al runner l'esito con
   `confirm_deal` e fallire rumorosamente su `REJECTED`. Mandare su Telegram la
   correzione (la posizione del 16/09 non esiste).
2. **Decidere lo strumento**: SVXY lungo è l'unica forma eseguibile del premio di
   volatilità su questo broker. Rifare pre-registrazione e gate su SVXY, con
   stop del broker e tetto, prima di toccare l'esposizione. Taglia: la tabella
   del §4 dice cosa costa ogni gradino in euro.
3. **Sistemare il job settimanale** (una riga).
4. **Grid**: non alzare la taglia. Togliere HK50 è l'unica mossa con una ragione
   oggettiva (costo certo). Se si vuole smettere di pagare il finanziamento di un
   sistema a attesa nulla, l'alternativa onesta è spegnerlo, non ingrandirlo.

## 7. Cosa NON dicono questi dati

Non dicono che il grid perda: dicono che non si distingue da zero e che paga
~19%/anno di finanziamento per starci. Non dicono che SVXY guadagnerà: dicono
che è l'unico strumento qui dentro con un premio strutturale misurabile, e che il
broker ne trattiene la maggior parte.

## 8. Esito (stesso giorno)

L'utente ha chiesto di procedere. Fatto:

- il runner della volatilità controlla `marketModes` prima dell'ordine e
  `/confirms` dopo; senza `ACCEPTED` l'ordine non conta e arriva un avviso;
- strumento `SVXY` comprato lungo, verso parametrico, stop dal lato giusto;
  nuova pre-registrazione in `gate-svxy-2026-10-09.md`, scala 15/20/25%
  invariata, gate 2027-01-07;
- `jobs/account_truth --weekly` di nuovo funzionante (proprietà `config` sul
  client Capital);
- HK50 fuori da entrambi i conti (profili RHK/DHK disabilitati, cron rimossi,
  paniere della pagina a sette indici). Il grid NON è stato ingrandito;
- stato locale `data/paura.json` azzerato, correzione pubblicata su Telegram e
  nel diario di claudetrade.eu.

La migration dello schema `vol` è stata applicata lo stesso giorno (psql sul
pooler) e lo schema esposto a PostgREST: su Supabase i nuovi schemi non sono
visibili all'API finché non compaiono in `pgrst.db_schemas` del ruolo
`authenticator`, e dopo ogni DDL serve `notify pgrst, 'reload schema'`. La
prima riga di misura in ombra è del 9 ottobre.
