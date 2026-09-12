# TradeAlert — Riepilogo completo del lavoro e dello stato

*Documento di riferimento. Aggiornato: 2026-07-26.*


> **Nota (2026-09-12)**: questo documento si ferma al 26 luglio 2026 e descrive la v2,
> oggi spenta. Per lo stato attuale e la storia aggiornata vedi [`storia-completa.md`](storia-completa.md).

Questo documento raccoglie tutto: la storia, i test, la transizione dalla versione 1
alla versione 2, la configurazione attualmente in produzione, i file, i backup e
cosa aspettarsi. È il punto della situazione definitivo.

---

## 1. Sintesi in due minuti

TradeAlert è un sistema di trading automatico su CFD (broker Capital.com), che gira
H24 su una VM, con un conto reale piccolo (~60 €) usato come laboratorio.

Il percorso, in una riga: **da un sistema che cercava di prevedere la direzione del
mercato con l'IA, a un sistema che ha accettato di non saperla prevedere e ha
iniziato a gestire l'esposizione con disciplina.**

Oggi è in produzione la **versione 2 (v2)**: un controller di esposizione che opera
solo sull'S&P 500 (US500), solo al rialzo, dosando quanti "blocchi" di esposizione
tenere in base alla volatilità. La versione 1 è spenta.

Il messaggio onesto di fondo: **il sistema non ha un vantaggio dimostrato sul
mercato.** Il suo valore è il metodo (misurare invece di illudersi) e una gestione
prudente dell'esposizione, non un edge magico.

---

## 2. L'evoluzione, per tappe

### v1 — Il sistema che sceglieva i trade

All'inizio TradeAlert scansionava ogni ora un paniere di asset, sceglieva "il setup
migliore" e apriva operazioni direzionali (long/short), con stop dinamici e un
monitor LLM per le uscite. La selezione usava un'**intelligenza artificiale** che
dava un voto a ogni candidato.

### La scoperta che ha cambiato tutto

Misurando, lo score dell'IA in ingresso aveva **correlazione ≈ 0** con gli esiti: non
prevedeva nulla. Sostituito da una regola deterministica gratuita ("il più forte del
giorno"), a parità di risultato: **costo LLM crollato dell'83%**.

### La lunga serie di "no"

Ogni leva di *selezione* e di *tempismo* testata è risultata nulla o dannosa: score,
volatilità, chasing, filtro di regime, frequenza di scan, frequenza del trailing,
calibrazione del trailing (artefatto), uscita a target fisso.

### La critica decisiva: era beta, non abilità

Scomponendo l'"edge" del paniere indici: +0,052R totali = +0,036R di semplice
"essere lungo" (beta) + 0,016R di residuo dentro il rumore. Su un backtest 2020-2026
che è stato in larga parte un mercato rialzista, il momentum long sugli indici **è
essere lungo, non è bravura**. Insieme: lo strumento (CFD) permette solo scommesse
direzionali; la direzione non è prevedibile; quindi si stava ottimizzando dentro uno
schema senza edge.

### v2 — La rivoluzione: gestire l'esposizione

Conseguenza logica: si smette di scommettere sulla direzione (falsificata) e si passa
a **gestire l'esposizione**. La volatilità, a differenza della direzione, ha una
struttura previsionabile. L'ipotesi non è più "dove va il prezzo" ma "quanto essere
esposti".

---

## 3. I test fatti (con esiti)

| Ambito | Ipotesi | Esito |
|---|---|---|
| Score LLM in ingresso | predice l'esito | **NO** (corr ≈ 0) → tolto, -83% costi |
| Volatilità / regime / chasing | filtri di selezione migliorano | **NO** (tutti) |
| Frequenza scan (ingresso) | scannerizzare più spesso aiuta | **NO** (piatto) |
| Frequenza trailing (uscita) | aggiornare più spesso aiuta | **NO** (piatto) |
| Calibrazione trailing | trail più stretto batte D | **Artefatto** di granularità |
| Monitor LLM (uscite) | distrugge valore | **NO**, non dannoso (ma non falsificabile) |
| Gap weekend / hold weekend | rischio sistematico | **Solo coda**, nessun edge |
| Paniere: quali asset | momentum ha edge ovunque | **Solo indici** (ma è beta) |
| Paniere: allargare | più asset = più occasioni | **NO** (diluisce) |
| **Decomposizione beta/alfa** | l'edge indici è abilità | **NO, è beta** su bull market |
| **v2: protezione profitto (trailing)** | tutela il guadagno | **DANNOSA** (erode e peggiora il drawdown) |

Il metodo dietro ogni riga: ipotesi pre-registrata, soglie fissate prima dei numeri,
misura in unità di rischio (R), controllo di robustezza, in-sample/out-of-sample.

---

## 4. Come funziona TradeAlert v2

**Il concetto: il blocco.** Un blocco è un'unità di esposizione da 20 € di margine.
Il sistema non sceglie "il setup migliore": calcola una volta al giorno **quanti
blocchi vuole tenere aperti** e ne apre/chiude per raggiungere quel numero. Se il
numero non cambia, non fa nulla e non paga commissioni.

**Un solo strumento, solo long.** Opera su US500 e basta (disciplina contro la
molteplicità che aveva prodotto i falsi positivi). Solo al rialzo (lo short misurava
zero, la deriva dell'indice è positiva).

**Quanti blocchi (volatility targeting).** Ogni sera misura la volatilità dell'S&P
(media mobile esponenziale, parametro fisso non calibrabile) e la confronta con un
obiettivo del 15% annuo: mercato calmo → più esposizione, mercato agitato → meno.

**Il tetto N_max.** Non è un parametro: è calcolato dall'equity del conto e dalla
leva reale, con un margine di sicurezza per sopravvivere a un gap del 10%. **A 60 € di
conto, N_max = 1**: il sistema opera nel regime {0, 1 blocco}. L'equity include il
guadagno flottante, quindi il conto cresce e a ~120 € di equity si sblocca il 2°
blocco, **automaticamente, senza dover uscire**.

**Isteresi.** Gli aumenti di esposizione aspettano 2 giorni di conferma (evita il
churn); le riduzioni di rischio sono immediate.

**Niente stop di trading, solo stop di catastrofe.** Il blocco non ha trailing né
take-profit (li chiude solo il controller). Ha uno stop di catastrofe a -7,5%:
assicurazione contro il crollo, non una regola di trading.

**Circuit breaker.** Se l'equity scende sotto 40 €, chiude tutto, si ferma, richiede
ripresa manuale.

**Benchmark ombra (obbligatorio).** Ogni giorno il sistema calcola anche cosa avrebbe
fatto "sempre 1 blocco" (il beta passivo) e "flat". Non si può leggere la performance
del controller senza vederla accanto al beta: il dubbio è cablato nello schema.

**Avviso di profitto.** Quando il guadagno flottante supera +15/+25/+40 % del conto,
il sistema **suggerisce** di valutare se consolidare, spiegando il trade-off. Non
chiude nulla: la decisione è dell'utente.

**Il ruolo dell'IA.** Ridotto a una sola cosa: un classificatore che stima la
**varianza** attesa (non la direzione) dal calendario macro. In v1 logga soltanto,
non applica (esperimento separato).

---

## 5. Configurazione in produzione (2026-07-26)

**Flag principali (.env sulla VM):**

| Flag | Valore | Significato |
|---|---|---|
| `V2_EXPOSURE_ENABLED` | true | v2 attivo, v1 spento |
| `V2_AUTO_EXECUTE` | true | il controller apre/chiude blocchi reali |
| `V2_EPIC` | US500 | strumento singolo |
| `BLOCK_MARGIN_EUR` | 20 | margine per blocco |
| `SIGMA_TARGET` | 0.15 | volatilità obiettivo (annua) |
| `MAX_SCALE` | 2.0 | tetto sullo scaling |
| `HYSTERESIS_DAYS` | 2 | banda morta sugli aumenti |
| `CATASTROPHE_STOP_PCT` | 0.075 | stop di catastrofe -7,5% |
| `KILL_EQUITY_FLOOR_EUR` | 40 | circuit breaker |
| `MACRO_SCALE_ENABLED` | false | il macro si logga, non si applica |
| `V2_PROFIT_ALERT_PCTS` | 15,25,40 | soglie avviso profitto |

`EWMA_LAMBDA` (0.94) e il warm-up (250 barre) sono **costanti non modificabili** nel
codice: la libertà di calibrarli è esattamente ciò che produce i falsi positivi.

**Cron sulla VM (TZ Europe/Rome):**

| Orario | Job |
|---|---|
| 07:00 | `macro_variance_class` — classifica la varianza (logga) |
| 09-22 (ogni ora) | `profit_alert` — avvisa se in forte guadagno |
| 23:30 | `exposure_controller` — calcola e applica l'esposizione |
| 23:45 | `shadow_ledger_update` — benchmark ombra |
| dom 20:00 | `exposure_report` — information ratio vs beta passivo |
| orario / 09:10 | `reconcile`, `health_check` |

**Conto:** ~60 € su Capital.com.

---

## 6. Numeri onesti da avere in testa

- **N_max = 1** a questa taglia: il sistema tiene 0 o 1 blocco.
- **1 blocco ≈ 400 € di nozionale** (20 € margine, leva reale ~20), cioè leva ~6,7×
  sul conto: se l'S&P fa ±1 %, il conto fa circa ±4 €.
- **Per il 2° blocco serve equity ~120 €**, cioè un guadagno di ~60 € = **S&P +15 %**
  (orizzonte di mesi, non giorni).
- **Simulazione di v2 su 6,5 anni di S&P**: +32,5 % cumulato ≈ **+4,4 %/anno**. Nello
  stesso periodo il semplice "compra e tieni" avrebbe fatto +63 % (~+7,8 %/anno).
  **v2 rende meno del mercato**: è più prudente, non più redditizio.
- **Perdita massima su un blocco**: ~30 € (stop di catastrofe -7,5 %).

---

## 7. File principali

- `src/exposure_config.py` — parametri v2 (costanti non calibrabili incluse).
- `src/volatility.py` — modello di volatilità (EWMA).
- `src/exposure_controller.py` — la logica pura (N_max, target, isteresi).
- `jobs/exposure_controller.py` — il job giornaliero che applica.
- `jobs/shadow_ledger_update.py` — benchmark ombra.
- `jobs/exposure_report.py` — report.
- `jobs/macro_variance_class.py` — classificatore di varianza.
- `jobs/profit_alert.py` — avviso di profitto.
- `jobs/test_exposure_v2.py`, `jobs/profit_lock_test.py` — test.
- `supabase/migrations/20260726120000_v2_exposure_blocks.sql` — le 8 tabelle v2.
- `docs/tradealert-v2-exposure-blocks.md` — la spec normativa.
- `docs/sprint9-profit-lock.md` — il test sulla protezione del profitto.

---

## 8. Backup e rollback

Ogni cambiamento è dietro flag e reversibile. Backup sulla VM:
`.env.bak-pre-v2`, `logs/crontab.bak-pre-v2.txt` (stato v1 completo), più i backup
intermedi (`.env.bak-pre-derisk`, `.env.bak-pre-indici`, ecc.).

**Rollback a v1**: `cp .env.bak-pre-v2 .env`, ripristina il crontab da
`crontab.bak-pre-v2.txt`, chiudi i blocchi aperti. Le tabelle v2 non si cancellano
(i dati raccolti hanno valore indipendente).

---

## 9. Cosa aspettarsi e cosa resta

**A breve:** il primo blocco reale si aprirà quando il target resta stabile 2 giorni
(verosimilmente entro pochi giorni di borsa aperta). Da lì il sistema modula
l'esposizione da solo, con poche azioni. Domenica arriva il primo report.

**Bug noto:** i P&L in euro nel database sono ancora inaffidabili (conversione
valuta) e vanno risincronizzati dalle transazioni Capital; per questo v2 lavora in
percentuali sul nozionale e marca gli euro come non riconciliati.

**Non ancora fatto (per scelta o priorità):** il veto interattivo sulle aperture
(ora c'è la notifica ma non la finestra di veto), la risincronizzazione dei P&L
storici, il simulatore delle curve continua/discretizzata, la revisione avversaria
automatica. Le tabelle sono pronte, si aggiungeranno quando serviranno.

**Sicurezza:** la password del database è stata condivisa in chat per applicare la
migration; va ruotata dal dashboard Supabase.
