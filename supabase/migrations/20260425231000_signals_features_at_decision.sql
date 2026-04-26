-- Aggiunge una colonna JSONB per persistere le feature tecniche al
-- momento della decisione LLM. Permette analisi retrospettive senza
-- dover ricostruire il contesto storico (RSI, ATR, slope, ecc.).
-- Migration additive: la colonna e' nullable, i record esistenti
-- restano validi.

alter table signals
  add column if not exists features_at_decision jsonb;
