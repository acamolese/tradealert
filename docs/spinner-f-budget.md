# TradeSpinner — F_BUDGET_POS: budget di leva per posizione (constants_log §6.2)

Data: 2026-07-30. Stato: APPROVATO (revisione avversaria sotto), deployato.

## Problema (misurato, `jobs/spinner_diag.py`)

Con equity di lavoro 100€ il target si fermava a UNA posizione (SW20) nonostante
17 eligible/giorno. Causa strutturale, non bug: per gli eligible `f_opt = net_adj/σ²`
è sempre > F_MAX_POS, quindi ogni posizione entrava alla leva massima (f≈0.9-1.0)
e il tetto aggregato F_MAX_ACCOUNT=1.5 ne conteneva una sola. MAX_POSITIONS=3 era
irraggiungibile per costruzione. Scarti del 29/07: 11-12 MAX_PER_CLASS (indici),
4 F_MAX_ACCOUNT (AUDCHF carry fuori per 0.07 di leva; VOO/SPY/TLT/IVV idem).

## Modifica

Nuova costante `F_BUDGET_POS = F_MAX_ACCOUNT / MAX_POSITIONS = 0.5`, usata SOLO
nella costruzione del portafoglio (`select_target`): ogni posizione punta a
f ≈ 0.5 invece di riempire F_MAX_POS.

Regole (in `src/spinner_odds.py::portfolio_size`):
- n unità ≈ F_BUDGET_POS / f_unit, cap al budget aggregato residuo e a F_MAX_POS;
- eccezione ticket lumpy: 1 unità anche sopra il budget se sta nei cap
  (senza questa, AUDCHF min 69€ su 100€ non entrerebbe MAI, vanificando lo scopo);
- G_MIN si valuta alla taglia APERTA davvero: se g(f_scelta) < 2%/anno la
  posizione non entra (n può salire dentro i cap per superare la soglia);
- le posizioni già in target hanno priorità e saltano l'isteresi (continuità
  buy&hold: un nuovo rank non fa churn su una posizione tenuta).

INVARIATI: la misura del tabellone (f_exec/g_exec alla taglia massima eseguibile,
storico comparabile), G_MIN, F_MAX_POS, F_MAX_ACCOUNT, MAX_POSITIONS,
MAX_PER_CLASS, isteresi d'ingresso (ora consecutiva davvero, fix separato).

## Perché è meglio (numeri board 2026-07-29, equity 100€)

Config vecchia: SW20 f 0.88, g conto +4.0%/anno.
Config nuova (stesso giorno): SW20 f 0.53 + VOO f 0.67 → somma g +4.6%; quando
AUDCHF conferma l'isteresi, il mix indice+carry FX rende disponibile un raro
elemento davvero decorrelato (σ 0.058 contro 0.13-0.15 degli indici).
A parità di tetto aggregato, g(f) concava premia spalmare: 2-3 × g(0.5-0.7)
batte 1 × g(0.9).

## Revisione avversaria (§6.2) — obiezioni e risposte

1. "Le posizioni sono correlate: 3 indici non diversificano, sommi g ma anche σ."
   Vero per gli indici, ed è il motivo per cui MAX_PER_CLASS=1 resta: il
   portafoglio è al più indice+stock/ETF+fx. La somma delle g NON modella la
   covarianza (limite dichiarato): il beneficio atteso è reale solo nella misura
   in cui le classi sono decorrelate. Con 3 posizioni della stessa classe sarebbe
   una frode contabile; così com'è, è un'approssimazione accettata.
2. "Più posizioni = più giri di spread e più churn."
   Spread e financing sono per-nozionale e già in net_adj (invarianti di scala).
   Il churn è limitato da: isteresi consecutiva in ingresso, priorità agli hold,
   uscita solo per non-eligibility. Metrica di churn nel gate sotto.
3. "A taglia piccola g(0.5) può scendere sotto G_MIN: apri posizioni deboli."
   No: G_MIN si valuta alla taglia effettiva; chi non la supera resta fuori
   (VOO a f 0.27 residuo viene scartato, testato).
4. "È curve-fitting sui dati del 29/07."
   No: F_BUDGET_POS non è calibrata (è l'identità F_MAX_ACCOUNT/MAX_POSITIONS,
   la scelta meno arbitraria disponibile) e la misura resta invariata.

## Gate forward (pre-registrato, si valuta al 2026-09-30)

- Metrica 1: nel ≥70% degli scan, somma g del target ≥ g della migliore singola
  posizione vecchia-config (calcolabile dal board storico, `spinner_diag`).
- Metrica 2 (churn): mosse dell'esecutore (aperture+chiusure) ≤ 2/settimana in
  media. Se il churn supera, la priorità-hold non basta e si rivede.
- Niente P&L in euro come criterio (demo, e pnl DB non riconciliato).
- FAIL su una delle due → rollback: `F_BUDGET_POS = F_MAX_POS` ripristina il
  comportamento precedente (una riga), con nuovo record constants_log.

## Note operative

- Registro DB: `jobs/spinner_constants_log.py` inserisce experiment +
  adversary_review + constants_log (FK §6.4). Eseguito sulla VM.
- Discrepanza rilevata (da tenere d'occhio, non parte di questa modifica): il
  min_size di SW20 in cache anagrafica (minNot ~17.7€) non coincide con quello
  fresco visto dall'esecutore (~77€, 1 lotto min 0.005). Probabile cambio del
  minDealSize lato Capital: il refresh settimanale della cache riallinea; se
  ricapita, valutare refresh più frequente. Effetto pratico: l'esecutore apre
  comunque a f reale ≤ F_MAX_POS (ricalcola col prezzo fresco), ma la f di
  portafoglio può risultare più alta del budget su ticket lumpy reali.
