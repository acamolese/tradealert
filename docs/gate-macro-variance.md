# Gate pre-registrato — macro_variance_class (v2 §5.1)

Pre-registrato: 2026-07-30, PRIMA di guardare qualunque esito. Valutazione:
quando il campione matura (vedi soglie), attesa ~fine settembre 2026.

## Cosa si sta testando

`jobs/macro_variance_class.py` (cron 07:00 IT, dal 2026-07-27) classifica ogni
mattina la VARIANZA attesa di US500 nelle 24h (low/medium/high) da calendario
macro, via LLM. Oggi LOGGA soltanto (MACRO_SCALE_ENABLED off). La domanda:
la classe anticipa la volatilità realizzata MEGLIO di quanto già faccia
l'EWMA che il controller usa? Se non aggiunge nulla oltre l'EWMA, è ridondante
e va spenta (costa una chiamata LLM/giorno e una superficie di complessità).

Contesto storico che impone il gate: ogni segnale predittivo testato in questo
progetto è risultato negativo (score LLM r=-0.02, regime filter rovesciato,
M4 walk-forward bocciato). La varianza è "l'unica previsione lecita"
dell'impianto v2, ma lecita non significa utile: va dimostrato.

## Dati

- Classificazioni: `monitoring_events` event_type=macro_variance, una per
  mattina, classe in details.impact. Data di riferimento d = data (Europe/Rome)
  della classificazione.
- Realizzato: ritorno log giornaliero US500 close-to-close del giorno d
  (candela DAY Capital, mid bid/ask).
- Baseline EWMA: σ giornaliera EWMA (λ=0.94, stessa del controller) calcolata
  fino a d-1 compreso. surprise_d = |ret_d| / σ_{d-1}: quanto il giorno ha
  sorpreso RISPETTO a quello che l'EWMA già sapeva.

## Criteri (decisi ora, non modificabili a posteriori)

Campione minimo: ≥ 40 classificazioni con bar abbinabile E ≥ 5 giorni "high"
(medium conta nel test di ordinamento ma non sblocca da solo).

- M1 (ordinamento grezzo): mediana |ret| dei giorni high > mediana |ret| dei
  giorni low.
- M2 (valore oltre l'EWMA, il criterio che conta): mediana surprise dei giorni
  high > mediana surprise dei giorni low. Se la classe non batte l'EWMA,
  duplicare l'informazione non serve al controller.

Verdetto:
- PASS = M1 e M2 entrambe vere → si può PROPORRE un esperimento MACRO_SCALE
  (mapping low/medium/high → 1.0/0.7/0.5), a sua volta pre-registrato e con
  flag separato. Il PASS NON attiva nulla da solo.
- FAIL una delle due → si archivia: rimuovere il cron macro_variance_class,
  MACRO_SCALE_ENABLED non si attiva mai.
- NON VALUTABILE: se dopo 12 settimane i giorni high sono < 5, il
  classificatore non discrimina (dice sempre low/medium) → equivale a FAIL.

## Esecuzione

`jobs/macro_variance_gate.py` (sola lettura): calcola le metriche e stampa lo
stato; con campione maturo emette il verdetto e avvisa su Telegram. Cron
settimanale domenica 20:10 IT (silenzioso finché immaturo, log in
logs/macro_gate.log).
