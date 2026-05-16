-- Bug #6 Tier 1: stati nuovi per gestire l'inconsistenza fra
-- create_position su Capital riuscita e insert_trade fallito.
--
--   execute_inconsistent  -> l'auto-executor ha sollevato un'eccezione
--                            dopo il timeout finestra. La posizione su
--                            Capital potrebbe essere aperta ma non
--                            persistita. Il position_monitor cerca di
--                            ricucire il link al ciclo successivo.
--   executed_recovered    -> il monitor ha trovato la posizione orfana
--                            e ricostruito il link signal->trade.
--                            Distinto da auto_confirmed per marcare il
--                            sample come "metodologicamente recovered":
--                            valido per validazione hit-rate ma con
--                            asterisco temporale (link a posteriori).
--
-- I valori legacy restano ammessi per i record storici.

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
    'cancelled_other',
    'execute_inconsistent',
    'executed_recovered'
  ));
