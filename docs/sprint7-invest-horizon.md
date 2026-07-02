# Sprint 7 — Test del cambio di orizzonte (trading → investimento sistematico)

Data: 2026-07-02. Stato: PRE-REGISTRATO prima dell'esecuzione.

## Ipotesi

M4 ha falsificato il trend-following a orizzonte swing sui costi CFD retail.
Ma la letteratura documenta da decenni che gli stessi premi (trend/momentum)
sopravvivono a orizzonte MENSILE con costi da ETF (~0.1% per switch invece di
spread CFD + overnight ogni notte). L'ipotesi da testare: sui nostri 11 anni
di dati giornalieri, le due strategie canoniche della letteratura mostrano il
premio? Se sì, la "rivoluzione" di TradeAlert è cambiare gioco: da scanner
CFD intraday a sistema di investimento sistematico mensile su ETF, dove
l'infrastruttura (VM, Telegram, disciplina, gate) resta e il costo crolla.

## Protocollo (parametri CANONICI dalla letteratura, ZERO ricerca parametri)

- **Faber GTAA (2007):** per ogni asset, a fine mese: investito se prezzo >
  SMA 10 mesi, altrimenti cash. Sleeve equal-weight.
- **Momentum 12-1 (Jegadeesh/Antonacci):** a fine mese, rank per rendimento
  degli ultimi 12 mesi escluso l'ultimo; long i top 3 se momentum positivo,
  altrimenti cash su quello slot.
- **Benchmark: buy & hold equal-weight** stesso universo.
- Universo FISSO (14 asset liquidi già scaricati, no FX): US500, US100, US30,
  DE40, UK100, J225, AU200, GOLD, SILVER, COPPER, OIL_BRENT, NATURALGAS,
  BTCUSD, ETHUSD. Un asset entra quando i suoi dati iniziano.
- Dati: candele DAY 2015-2026 (mid bid/ask), ribilanciamento all'ultimo
  giorno di borsa del mese. Costo 0.10% per cambio di stato di uno sleeve.
- Metriche: CAGR, max drawdown, Sharpe (mensile annualizzato, rf=0), anno
  peggiore, split 2015-2020 / 2021-2026.

## Caveat dichiarati PRIMA di guardare i numeri

1. Prezzi CFD price-return: gli indici NON includono i dividendi (~2-3%/anno
   che un ETF reale incasserebbe). I risultati equity sono quindi SOTTOSTIMATI.
2. Un run solo, parametri fissi. Se il risultato è deludente NON si ritoccano
   i parametri (sarebbe il round 3 vietato).
3. Questo test giudica la DIREZIONE (il gioco mensile ha premio positivo?),
   non promette il numero esatto: l'implementazione reale sarebbe su ETF
   UCITS con total return, non su CFD.

## Criterio di lettura (pre-registrato)

- Il cambio di gioco è SUPPORTATO se almeno una delle due strategie batte
  cash (CAGR > 2%) E ha max drawdown < buy&hold E Sharpe ≥ 0.5 sull'intero
  periodo, con entrambe le metà del periodo positive.
- Altrimenti: anche il gioco mensile è da archiviare su questi dati, e la
  risposta onesta a "rivoluzionare TradeAlert" diventa: nessuna via
  sistematica interna; ridimensionare (paper/hobby) o spegnere.
