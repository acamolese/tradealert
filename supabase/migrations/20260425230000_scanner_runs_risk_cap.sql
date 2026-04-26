-- Estende l'enum di scanner_runs.outcome per includere 'risk_cap',
-- usato quando il drawdown cap settimanale ferma lo scanner
-- (vedi src/scanner.py::_check_drawdown_cap).

alter table scanner_runs drop constraint scanner_runs_outcome_check;

alter table scanner_runs add constraint scanner_runs_outcome_check
  check (outcome in (
    'no_data',
    'no_setup',
    'slots_full',
    'rotation_proposed',
    'signal_sent',
    'risk_cap'
  ));
