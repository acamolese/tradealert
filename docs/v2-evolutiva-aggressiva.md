# v2 — Evolutiva aggressiva 2026-08-01 (micro-blocchi + target 0.20 + floor 25€)

Decisione utente: "più aggressivo nel RISCHIO, non nel capitale" (nessun deposito).
Analisi: `jobs/exposure_aggressive_test.py` (replica ESATTA della meccanica a
blocchi: N_max da equity dinamica, isteresi 2gg, riduzioni immediate, kill floor
permanente), US500 daily 14.1 anni, partenza dall'equity reale 55.63€.

## Il problema che ha motivato l'evolutiva

Due difetti strutturali alla taglia attuale del conto, entrambi riprodotti nel
backtest:
1. STALLO: con BLOCK_MARGIN=20€ (blocco 400€ nozionale) il primo blocco richiede
   equity >= 60€. Dopo la prima perdita reale (55.63€) N_max=0 per sempre: flat
   non si rientra mai. Nel backtest: 0 mosse in 14 anni.
2. KILL FLOOR troppo vicino: a 40€ dista -28% dall'equity, e OGNI config con
   blocchi >= 200€ lo buca al primo drawdown serio -> HALT permanente.

## Config deployata (F) e alternative testate

| config (da 55.63€, floor 25€) | CAGR | maxDD | %inv | mosse/14a |
|---|---|---|---|---|
| E micro m5 st.15 ms2          | +4.10% | 41% | 90% | 97  |
| **F micro m5 st.20 ms3 (SCELTA)** | **+4.43%** | **49%** | **91%** | **128** |
| G micro m5 st.25 ms3 g.05     | +4.80% | 57.5% | 92% | 154 |
| B/C/D blocchi 200€ (ogni floor)| morte  | -     | ~2% | HALT |

Cambi (.env VM, prod_config_change nel DB):
- BLOCK_MARGIN_EUR 20 -> 5 (blocco ~100€ nozionale: sblocca lo stallo,
  granularità 0/100/200/300€ invece di 0/400)
- SIGMA_TARGET 0.15 -> 0.20 (più esposizione a parità di volatilità)
- MAX_SCALE 2.0 -> 3.0 (fino a 3 blocchi quando sigma < 0.067)
- KILL_EQUITY_FLOOR_EUR 40 -> 25 (accettazione esplicita del rischio: il conto
  può dimezzarsi prima dell'HALT; storicamente F non ha mai toccato 25€)

INVARIATI: GAP_TOLERANCE=0.10 (la sopravvivenza al gap NON si indebolisce: G
comprava +0.37%/anno pagando 8 punti di DD e un vincolo di sicurezza),
EWMA/warmup/isteresi/catastrophe stop 7.5%, solo-long US500.

## Cosa aspettarsi

- N_max oggi: floor(55.63/15) = 3. Sigma ~0.125 -> scale 1.6 -> target 2 blocchi
  (~200€). Isteresi 2gg: prima apertura al secondo scan con target stabile
  (weekend di mezzo: realisticamente lunedì 3/8 sera, o al primo scan con
  mercato aperto).
- Rispetto al vecchio 0/400€: esposizione media storica ~160€ ma 91% del tempo
  investito e de-risk a gradini (400->200->100->0) invece che tutto-o-niente.
- ONESTÀ SUI NUMERI: +4.4%/anno storico è MENO del buy&hold (+11.9% con DD 129%,
  cioè conto azzerato: non sopravvive). Il controller compra sopravvivenza
  pagando rendimento. Più aggressivo di così, a QUESTO capitale, nel backtest
  muore. La leva che il backtest premia davvero è il capitale (a 120€ la
  config a blocchi 400€ fa +6.4%/anno): resta la strada se si cambia idea.

## Rollback

Ripristinare nel .env i 4 valori precedenti (backup .env.bak-pre-aggressive) e
registrare un nuovo prod_config_change. Nessun cambio di codice.
