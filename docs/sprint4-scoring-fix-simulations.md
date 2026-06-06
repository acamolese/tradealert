# Sprint 4 — Simulazioni offline fix scoring

Data: 2026-06-06. Mercato fermo, **nessun deploy, nessuna modifica al
sistema live** (monitor invariato). Misure su dati storici/sintetici per
decidere il design del fix scoring emerso da `docs/scoring-stability-analysis.md`
prima di scrivere codice. Modello: Sonnet 4-6 (lo scorer di produzione).

## Sim A — La temperatura bassa collassa il jitter? SÌ

Stesso input (universo Gold+Nasdaq+Bitcoin), 10 run per condizione.

| asset | GROUP temp 1.0 (sd, range) | GROUP temp 0.2 | PER-ASSET temp 0.2 |
|-------|---------------------------|----------------|--------------------|
| Gold | 0.20, 7.0-7.5 | 0.20, 7.0-7.5 | — |
| Nasdaq | **0.62, 4.5-6.5** | 0.20, 6.0-6.5 | **0.00, 6.5** |
| Bitcoin | **0.71, 4.0-6.2** | **0.00, 4.5** | — |

A temp 0.2 il jitter crolla: Nasdaq da sd 0.62 a 0.20, Bitcoin da 0.71 a
0.00. Per-asset a temp 0.2 è di fatto **deterministico** (sd 0.00). Il
jitter ±1,5 osservato era quindi un artefatto della temperatura 1.0
(ereditata dalla generazione della prosa). **Conferma piena**: temp bassa
stabilizza lo score e renderebbe stabile la soglia 7.

Conseguenza operativa: per stabilizzare gli score in Sim C bastano pochi
run. Con sd≈0 a temp 0.2 ho usato **N=5** (la media è già stabile; N=10
sarebbe stato ridondante). Deviazione dichiarata dal protocollo N≥10,
giustificata dal risultato di Sim A.

## Sim B — La valutazione per-asset toglie la dipendenza dall'universo? SÌ (per costruzione), ma non discrimina meglio

- **Composizione**: Sim A mostra Nasdaq a **6.1 in gruppo** (con Gold+Bitcoin)
  vs **6.5 da solo**. Valutare l'asset isolato elimina il "drag" competitivo
  (~0.4-0.5), per costruzione. Confermato il Finding 1.
- **Discrimina meglio?** Vedi Sim C: gli score per-asset puliti NON separano
  meglio WIN/LOSS su Sprint 2. Quindi togliere la dipendenza dall'universo
  è metodologicamente corretto ma **non porta il guadagno di qualità entry
  che speravamo** sul sistema attuale.

## Sim C (la madre) — Lo score pulito ri-discrimina? Risultato MISTO, negativo sul sistema attuale

Per ogni trade: ri-scoring **per-asset, temp 0.2, N=5, media** ("score
pulito"). Due viste tenute separate.

### Sprint 2 (sistema attuale, bidirezionale, feature complete) — NON discrimina

| split | score storico (group, temp1.0) | score pulito (per-asset, temp0.2) |
|-------|-------------------------------|-----------------------------------|
| rapid_loss vs developed (mediana) | 7.25 vs 7.00 → **−0.25** (perverso) | 6.5 vs 6.0 → **−0.5** (perverso, peggio) |
| WIN vs LOSS (mediana) | ~0 | 6.25 vs 6.10 → **+0.15 (≈0)** |

Lo score pulito su Sprint 2 **non separa WIN da LOSS** (≈0) e mantiene
(anzi amplifica) il pattern perverso rapid-loss > developed. Pulire lo
score **non risolve il problema entry sul sistema attuale**.

### Sprint 1 (long-only, feature ridotte, sistema vecchio) — discrimina

| split | score storico | score pulito |
|-------|--------------|--------------|
| WIN vs LOSS (mediana) | 7.0 vs 7.2 → ~0 | **WIN 6.4 vs LOSS 5.25 → +1.15** |

Su Sprint 1 lo score pulito separa WIN da LOSS nella direzione GIUSTA
(+1.15 di mediana), dove lo storico non separava (≈0). Qui il fix
"funzionerebbe".

### Lettura

Il fix scoring (temp bassa + per-asset + stabilizzato) **non è la
soluzione del problema entry sul sistema corrente (Sprint 2)**: lo score,
anche pulito, non predice gli esiti dei trade Sprint 2. Funziona su
Sprint 1, che però è un sistema diverso (long-only, feature ridotte) e non
generalizzabile.

Questo **conferma e rafforza Sprint 3.5**: i rapid loss si distinguono per
regime intraday (volatilità, asset debole nella giornata), feature che lo
score non incorpora. Ridurre il rumore dello score non aggiunge il segnale
mancante. Quindi: il rumore (Finding 2) e la dipendenza dall'universo
(Finding 1) sono reali e vanno sistemati per stabilità/costo, ma **la
qualità dell'entry su Sprint 2 è altrove** (nelle feature/nel cosa lo
score ottimizza), non nel rumore dello score.

## Sim D — Costo della valutazione per-asset: ~2× rispetto al gruppo

Token per chiamata (prompt caching system attivo, cacheR ~4928):

| modalità | chiamate per scan | out/call | costo stimato per scan |
|----------|-------------------|----------|------------------------|
| gruppo (attuale) | 1 | ~1043 | ~$0.019 |
| per-asset | 5 (una per asset) | ~400 | ~$0.041 |

Il per-asset paga 5× la lettura cache del system prompt e ~2× l'output
totale (ogni chiamata rifà la struttura JSON). Sul costo scanner (~$0.46/gg)
significa **circa raddoppiarlo (~$0.9/gg)**. Il caching attenua ma non
annulla il penalty.

## Raccomandazione di design

1. **Temperatura bassa per lo scoring (temp ~0.2): SÌ.** Cheap win:
   collassa il jitter (Sim A), stabilizza la soglia 7, costo invariato.
   Richiede di separare score (temp bassa) dalla thesis (temp alta per la
   prosa) — converge con la Leva 1/2 costo token. Basso rischio.
2. **Valutazione per-asset: NON giustificata ora.** Toglie un artefatto di
   composizione di ~±0.5 (utile in linea di principio) ma **raddoppia il
   costo** (Sim D) e **non migliora la discriminazione** sul sistema
   attuale (Sim C/S2). Rimandare finché non c'è un guadagno entry
   dimostrato.
3. **Il fix entry NON è il denoising dello score.** La simulazione madre
   dice che su Sprint 2 lo score pulito non separa WIN/LOSS. Sprint 4
   dovrebbe puntare su **arricchire le feature** (regime intraday:
   volatilità, posizione nel range giornaliero — i discriminatori di
   Sprint 3.5) o ripensare cosa lo score ottimizza, non solo ridurne il
   rumore.

In sintesi: fare la temp bassa (stabilità/costo), **non** fare il
per-asset, e spostare l'obiettivo "qualità entry" dalla pulizia dello
score all'arricchimento delle feature.

## Limiti (sample)

- Universi sintetici (feature dell'ultimo signal per asset, da timestamp
  diversi); score assoluti non identici a quelli storici.
- Campioni piccoli: Sprint 2 6 WIN / 14 LOSS, Sprint 1 6 WIN / 12 LOSS.
  Nessun test di significatività regge; si leggono direzioni e mediane.
- Sprint 1 = sistema diverso (long-only, feature ridotte: niente
  trend_slope_short_pct ecc.): l'estensione aumenta il sample ma introduce
  eterogeneità, per questo tenuto separato.
- Split non omogenei tra i due: S2 rapid/developed (peak-based) + WIN/LOSS,
  S1 solo WIN/LOSS (pnl).
- N=5 (non 10) giustificato da Sim A (sd≈0 a temp 0.2).
- Feature storiche ricostruite. Tutto preliminare, da riconfermare con uno
  shadow test in produzione sui trade post-fix.
