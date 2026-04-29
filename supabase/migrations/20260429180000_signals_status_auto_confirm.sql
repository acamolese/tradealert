-- Allarga il check constraint su signals.status per accogliere i nuovi
-- valori introdotti dalla modalita' auto-confirm con finestra skip:
--   auto_confirmed         -> apertura completata via timeout (ex 'executed')
--   manual_skipped         -> utente ha cliccato il bottone Skip
--   cancelled_rr_degraded  -> R:R sotto soglia al recheck pre-volo
--   cancelled_risk_cap     -> cap settimanale drawdown raggiunto
--   cancelled_max_positions-> slot MAX_OPEN_POSITIONS gia' saturo
--   cancelled_other        -> qualsiasi altro errore tecnico (es. epic 404)
--
-- I valori legacy (pending, executed, skipped, expired) restano ammessi
-- per i record storici creati prima del 2026-04-29.

alter table signals drop constraint if exists signals_status_check;

alter table signals add constraint signals_status_check
  check (status in (
    'pending',
    'executed',
    'skipped',
    'expired',
    'auto_confirmed',
    'manual_skipped',
    'cancelled_rr_degraded',
    'cancelled_risk_cap',
    'cancelled_max_positions',
    'cancelled_other'
  ));
