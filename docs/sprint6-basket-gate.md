# Sprint 6 A3 — Gate paniere nuovi asset (esito)

Data esecuzione: 2026-07-15 (trigger agenda: 17 trade chiusi ≥ soglia 15).
Gate pre-registrato in `docs/sprint6-piano-scalata.md` sez. A3.

Nota metodo: le R sono calcolate DAI PREZZI (close vs entry, normalizzate
sullo stop del signal), non da `trades.pnl`, perché 3 record (#89, #106,
#118, tutti Hang Seng chiusi da reconcile) hanno il pnl corrotto dal bug
di conversione HKD (fix ancora da applicare). Il calcolo dai prezzi è
immune alla valuta.

## Verdetto: I NUOVI ASSET RESTANO (nessun rollback)

Finestra "stesso periodo": dal primo trade su asset nuovo (2026-06-25),
46 trade chiusi, 0 esclusi.

| Gruppo | n | exp_R |
|---|---|---|
| Nuovi (FX + Copper/HangSeng/Nikkei) | 17 | **-0.024** |
| Core (Gold, Brent, US500, Nasdaq, Bitcoin) | 29 | **-0.050** |

Trigger di rollback pre-registrato: nuovi < core − 0.10R → richiedeva
exp_R(nuovi) < -0.150. Osservato -0.024 (delta +0.025 A FAVORE dei nuovi):
condizione lontana, si applica il ramo "i nuovi restano".

## Dettaglio e onestà sul campione

- Per asset: Hang Seng +0.314 (n=6), AUD/USD +0.378 (n=2), Copper -0.217
  (n=6), GBP/USD -0.601 (n=2), EUR/USD -0.550 (n=1), **Nikkei n=0**.
- Anti-outlier: senza i 2 migliori dei nuovi l'exp scende a -0.168 (sotto
  la soglia), senza i 2 peggiori sale a +0.087. Con n=17 il confronto è
  fragile in entrambe le direzioni; il gate però richiede il trigger sul
  campione pieno e il trigger non c'è. Nessuna azione è il default.
- **Nikkei non ha MAI aperto un trade**: margine minimo broker ~€338 vs
  budget massimo €30. Con l'entrata LLM ogni scan su Nikkei costava token;
  con la Fase B il costo di scan è ~zero, quindi resta in universo come
  peso morto innocuo. Da rivalutare solo se si scala il capitale (A4).

## Azioni

- Nessun cambio a produzione: `BASKET_FX_ENABLED`/`BASKET_TREND_ENABLED`
  restano true.
- Il FREEZE sui *futuri* asset resta in vigore fino al primo step di
  scaling (A4), come pre-registrato.
- Prossima revisione del paniere: alla finestra A4 (50 trade) o su
  richiesta.
