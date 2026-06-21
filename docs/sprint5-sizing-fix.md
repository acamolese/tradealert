# Sprint 5 (pre-requisito) — Fix sizing currency-aware

Data: 2026-06-21. Bugfix gated, reversibile. Tocca il sizing di TUTTI gli asset
→ massima cura. **Nessun merge in produzione finché non leggiamo il doc insieme.**
Codice: `src/risk.py`, `src/executor.py`, `src/config.py`. Flag:
`SIZING_CURRENCY_AWARE` (default OFF = bit-identico).

**Sintesi in una riga**: il sizing diventa currency-aware con riferimento **USD**
(non EUR); i 5 asset esistenti restano **bit-identici al centesimo** (provato),
USD/JPY passa da **rifiutato a 400 unità** sensate; gli USD-quoted FX restano
identici agli asset USD esistenti (vedi la decisione sotto).

---

## Il bug

`calculate_size` calcolava `risk_per_unit = entry_price × stop_pct/100` in
**valuta quotata** e lo confrontava col cap (EUR) **senza conversione**. Corretto
solo se quote ≈ valuta del cap. Conseguenza (da `sprint5-basket-concentration.md`):
USD/JPY rotto (quote JPY trattato 1:1 → fattore ~175 → 80€ di rischio stimato su
size minima → trade **sempre rifiutato**); quote USD con errore ~15%.

## La conversione (Task 1)

`risk_per_unit` (e le grandezze derivate da entry: notional, margine) vanno
portate in valuta di riferimento moltiplicando per `quote_to_ref`:

```
entry_ref = entry_price × quote_to_ref
risk_per_unit = entry_ref × stop_pct/100      # poi confrontato col cap
notional      = size × entry_ref               # poi × margin_factor per il margine
```

Con `quote_to_ref = 1.0` → `entry_ref = entry_price` → **calcolo identico a
prima** (path OFF e asset USD).

## La DECISIONE chiave: riferimento USD, non EUR (leggere)

C'è un **conflitto interno** tra due richieste del task, che vanno chiarite:
"bit-identico sui 5 attuali" e "USD-quoted FX corretti dal +15%" **non possono
coesistere**, perché i 5 asset attuali (Gold, Brent, US500, Nasdaq, Bitcoin) e gli
FX USD-quoted (EUR/USD, GBP/USD, AUD/USD) sono **la stessa valuta quotata (USD)**.
Correggere il 15% sugli uni cambia anche gli altri.

La scelta dipende dalla valuta di **riferimento** della conversione:

| | Riferimento EUR (da manuale) | **Riferimento USD (scelto)** |
|--|------------------------------|------------------------------|
| 5 asset attuali (USD) | cambiano ~+15% (size più grande) | **invariati, bit-identici** |
| FX USD-quoted | "corretti" +15% | identici agli asset USD |
| USD/JPY | sbloccato | **sbloccato** |
| R storico, baseline V1 −0.42R, figure € | **si spostano del 15%** | **coerenti, intatti** |
| bit-identico (gate hard) | **violato** | **rispettato** |

**Ho scelto il riferimento USD**, per tre ragioni:
1. Il bit-identico è un **gate hard** ("prova prima del merge"): il riferimento
   EUR lo violerebbe su tutti e 5 gli asset.
2. R è stato **de-facto USD-denominato** per tutta la storia (ogni asset quote=USD
   trattato 1:1 col cap). Tenere USD mantiene coerenti baseline V1, gate, e tutte
   le figure € passate. Il riferimento EUR le sposterebbe del 15%, rendendo i
   trade nuovi non confrontabili coi vecchi.
3. Il "15% sugli USD-quoted" è una **convenzione uniforme** (ogni asset uguale),
   non un bug per-asset. "Correggere" solo gli FX li renderebbe **incoerenti** con
   Brent/Gold (peggio, non meglio). Il cap effettivo è "5 USD ≈ 4,35 €" per
   **tutto**, in modo uniforme: accettabile e consistente.

Quindi il fix corregge **solo il mismatch grossolano** (quote non-USD: JPY, CHF),
lasciando gli USD-quoted (esistenti + FX) esattamente come oggi. Se in futuro si
volesse passare a EUR-vero come riferimento, è un cambio separato che
ri-denomina R per l'intero sistema, da fare consapevolmente — non in questo fix.

## Sorgente del tasso + fallback (Task 2)

Sorgente: `instrument.currency` (campo Capital affidabile, verificato: EURUSD→USD,
USDJPY→JPY, USDCHF→CHF, e tutti gli asset attuali→USD). Il tasso quote→USD si
legge dall'epic appropriato (`_QUOTE_TO_USD_EPIC` in `risk.py`): JPY→USDJPY
(invertito), CHF→USDCHF (invertito), GBP→GBPUSD, EUR→EURUSD, AUD→AUDUSD.

**Fallback esplicito** (`quote_to_ref_factor` ritorna `None`, il chiamante
rifiuta il trade):
- quote == USD (o assente) → `1.0`, **nessun fetch** (gli asset USD non hanno
  modo di fallire → bit-identico garantito).
- quote non mappata (es. SEK) → `None` → **trade rifiutato**.
- fetch del tasso fallito / prezzo nullo → `None` → **trade rifiutato**.

Verificato live: `SEK→None`, `JPY con rete giù→None`, `USD→1.0`, `None→1.0`. Mai
crash, mai ripiego silenzioso sul calcolo rotto: **meglio non aprire che aprire
mal dimensionato.**

## Task 3 — Rete bit-identica (PROVA, mercato live 21/06)

OLD = formula pre-fix (entry_price ovunque). NEW-OFF = codice nuovo con flag OFF
(`quote_to_ref=1.0`). Budget 15€, cap 5€, stop 0.5%.

| epic | quote | OLD size | OLD risk | NEW-OFF size | NEW-OFF risk | match |
|------|-------|----------|----------|--------------|--------------|-------|
| GOLD | USD | 0.07 | 1.45 | 0.07 | 1.45 | ✅ IDENTICO |
| OIL_BRENT | USD | 3.7 | 1.48 | 3.7 | 1.48 | ✅ IDENTICO |
| US100 | USD | 0.009 | 1.36 | 0.009 | 1.36 | ✅ IDENTICO |
| US500 | USD | 0.04 | 1.50 | 0.04 | 1.50 | ✅ IDENTICO |
| BTCUSD | USD | 0.0004 | 0.13 | 0.0004 | 0.13 | ✅ IDENTICO |

Inoltre, provato a livello unità: `calculate_size(...)` con arg di default ==
con `quote_to_ref=1.0` esplicito (oggetti uguali). **Bit-identico garantito per
costruzione** (`entry_ref = entry_price × 1.0 = entry_price`) e confermato numerico.

## Task 4 — Correzione FX + USD/JPY sbloccato (flag ON, live 21/06)

| epic | quote | OLD (rotto) | fattore quote→USD | NEW-ON size | NEW-ON risk (USD) |
|------|-------|-------------|--------------------|-------------|--------------------|
| EUR/USD | USD | 300 | 1.0 | 300 | 1.72 |
| GBP/USD | USD | 300 | 1.0 | 300 | 1.99 |
| AUD/USD | USD | 600 | 1.0 | 600 | 2.10 |
| **USD/JPY** | JPY | **None (rifiutato)** | **0.0062** | **400** | **2.00** |
| USD/CHF | CHF | (sotto-dimensionato) | 1.2388 | 400 | 2.00 |

- **USD/JPY sbloccato**: entry ~161.3, stop 0.5% → rischio reale `161.3 × 0.5% ×
  (1/161.3) = 0.005 USD/unità`; size **400 unità**, rischio **2.00 USD ≈ 1,74 €**,
  margine ~13 USD entro budget. Da "rifiutato a ogni scan" a size sensata. Il
  fattore ~175 è sparito.
- **USD-quoted FX (EUR/USD, GBP/USD, AUD/USD)**: fattore 1.0 → **identici al
  comportamento attuale**, trattati come gli asset USD esistenti (coerente con la
  decisione riferimento-USD). NON "corretti del 15%" — di proposito.
- **USD/CHF**: CHF vale 1.24 USD, prima sotto-dimensionato, ora corretto (parcheggiato
  comunque per spread alto).

## Cosa è toccato (e cosa NO)

Toccato, solo questo commit:
- `src/risk.py`: `quote_to_ref_factor()` + `_QUOTE_TO_USD_EPIC` + param
  `quote_to_ref` (default 1.0) in `calculate_size` (entry→entry_ref).
- `src/executor.py`: calcolo del fattore gated da `SIZING_CURRENCY_AWARE`, con
  reject se tasso non disponibile; passa `quote_to_ref` a `calculate_size`.
- `src/config.py`: flag `sizing_currency_aware` (default False).

**NON toccato**: paniere (nessun FX aggiunto all'universo), tetto di
concentrazione, V1/trailing, prompt, soglie, monitor. Il preview Telegram
(`_preview_sizing` nello scanner) resta sul calcolo OFF: è solo una stima a video
e oggi gli FX non sono nel paniere, quindi non mostra mai FX; da allineare quando
gli FX andranno live (cosmetico, l'apertura reale usa l'executor già corretto).

## Guardrail

- **Flag** `SIZING_CURRENCY_AWARE` (default OFF). Rollback: `=false` nel `.env`
  VM → al ciclo successivo il sizing torna al fattore 1.0, bit-identico.
- **Nessun trade aperto toccato**: il sizing agisce solo all'apertura di nuovi
  trade.
- **OFF = comportamento storico esatto** (provato): accendibile senza rischio per
  gli asset USD; cambia solo i quote non-USD (che oggi non sono nel paniere → con
  flag ON e paniere attuale, **zero effetto pratico** finché non si aggiungono FX).

## Pronto per lunedì? Stima onesta

**Sì, il fix è pronto e verificato.** Il core è provato (bit-identico sui 5,
USD/JPY sbloccato, fallback corretto), gated, reversibile. Ma è **disaccoppiato**
dal go-live: con paniere attuale (solo asset USD) e flag ON, l'effetto è **nullo**
finché non si aggiungono FX. Quindi:

- **Lunedì può partire il solo tetto di concentrazione** (B1/B2,
  `sprint5-basket-concentration.md`), che non dipende dal sizing.
- Il fix sizing si **accende insieme agli FX** (quando il paniere li include),
  dopo aver letto questo doc insieme e ri-misurato gli spread FX in sessione.

Nessuna fretta di accendere `SIZING_CURRENCY_AWARE` prima degli FX: è un
pre-requisito pronto, non un blocco per lunedì.

## Limiti

Tassi di conversione letti a mercato chiuso (weekend) — in produzione si leggono
live al momento del sizing. La conversione assume `notional = size × entry` come
proxy del margine (già così nel codice pre-fix, e gli asset USD restano
bit-identici, quindi l'assunzione non peggiora nulla). Riferimento USD scelto per
continuità di R: una futura ri-denominazione in EUR-vero è un cambio separato.
