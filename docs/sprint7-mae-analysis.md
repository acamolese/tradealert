# Sprint 7 — Analisi MAE: esiste una soglia di non-ritorno prima dello SL?

Data: 2026-07-03. Stato: PRE-REGISTRATO prima dell'esecuzione.
Origine: intuizione dell'utente ("se un setup arriva a metà dello SL quasi
sicuramente non torna in guadagno; il sistema dovrebbe suggerire l'uscita").

## Due domande distinte (e possono avere risposte opposte)

1. **Descrittiva**: dato che un trade tocca -X (in R), qual è la probabilità
   che finisca comunque in profitto? (l'intuizione dell'utente)
2. **Di policy**: uscire d'ufficio a -X invece che a -1R migliora
   l'expectancy? Attenzione: la 1 può essere "quasi mai torna" (85% dei
   casi) e la 2 comunque negativa, perché i pochi che tornano valgono +2R
   mentre il risparmio è solo 0.5R a testa. Decide l'aritmetica, non la
   frequenza.

## Protocollo (un run, candele HOUR locali, entrate reali)

Stream: i trade chiusi con signal collegato (~82, gli stessi del test
convessità). Due viste:

- **Vista A (ciò che l'utente vede)**: traiettoria dall'apertura alla
  chiusura REALE (incluse le chiusure monitor). MAE = peggior escursione in
  R. Tabella per soglia X ∈ {0.25, 0.4, 0.5, 0.6, 0.75}: n che toccano -X,
  % che chiudono comunque >0, exit_R medio condizionato.
- **Vista B (policy, sim-vs-sim)**: baseline V0 = bracket con TP + trailing
  live (già validato M3, orizzonte 15gg). Varianti KILL-X per X ∈
  {0.4, 0.5, 0.6, 0.75}: identiche a V0 ma con pavimento dello stop a -X
  (equivale a "esci appena tocca -X"). Stesse candele, stesse assunzioni.

## Criterio di lettura (pre-registrato)

- **POLICY SUPPORTATA** se una variante KILL-X batte V0 di ≥ +0.15R di
  expectancy E il vantaggio resta ≥ +0.10R togliendo i 2 trade dove la
  variante guadagna di più (anti-outlier).
- Esito positivo → NON deploy diretto: flag `SL_FLOOR_R` gated forward
  (il monitor LLM già copre parte di questo spazio: verificare
  sovrapposizione prima dello switch).
- Esito negativo → si archivia; resta la tabella descrittiva come risposta
  quantitativa all'intuizione (utile comunque per la fiducia nel monitor).
- Dichiarato: 5° test dell'harness, 4 soglie provate = multiplicità; un
  positivo marginale non basta, serve il margine pieno.

## ESEGUITO 2026-07-03 — intuizione CONFERMATA, policy hard-exit NO

78 trade analizzati (script `jobs/mae_analysis.py`).

**Vista A (vita reale, ciò che l'utente vede):**

| tocca | n | chiude >0 | chiude >+0.5R | exit medio |
|---|---|---|---|---|
| -0.25R | 54/78 | 17% | 13% | -0.18R |
| -0.40R | 46/78 | 9% | 7% | -0.32R |
| **-0.50R** | **36/78** | **3% (1 trade)** | **0%** | **-0.49R** |
| -0.60R | 26/78 | 0% | 0% | -0.58R |

L'intuizione "a metà SL non torna" è confermata: a -0.5R il recupero sopra
zero è 1/36 e NESSUNO è mai tornato oltre +0.5R. Ma il dato chiave è l'exit
medio condizionato: **-0.49R**. Il sistema live (monitor + trailing) sta GIÀ
incassando in media proprio il valore dell'uscita a metà SL: l'intuizione
dell'utente è corretta ed è già in produzione, implementata dal monitor
(coerente con A1).

**Vista B (policy pavimento stop a -X, sim vs sim):** KILL-0.5 +0.035R,
KILL-0.6 +0.048R vs bracket: direzione giusta ma sotto la soglia +0.15R
pre-registrata, e con 4 soglie provate un marginale non basta. NON si cambia
lo SL. Nota: il margine residuo teorico della hard-rule vs monitor è
piccolo perché il monitor già copre lo spazio.

**Azione (notifica-only, deployata):** avviso Telegram una-tantum per trade
quando tocca -0.5R, dal loop trailing 5-min (event_type `mae_alert` come
dedup, rispetta quiet hours). Dà all'utente il segnale che chiedeva, coi
numeri storici nel messaggio, senza cambiare alcuna regola di trading.
