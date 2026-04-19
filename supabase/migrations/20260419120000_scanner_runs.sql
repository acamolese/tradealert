-- Traccia l'esito di ogni run dello scanner cosi' /status puo' spiegare
-- perche' non e' arrivata una notifica (nessun setup, slot pieni, ecc.).

create table scanner_runs (
  id bigserial primary key,
  ran_at timestamptz not null default now(),
  outcome text not null check (outcome in (
    'no_data',
    'no_setup',
    'slots_full',
    'rotation_proposed',
    'signal_sent'
  )),
  top_asset text,
  top_score numeric,
  candidates_count int,
  open_positions_count int,
  notes jsonb
);

create index idx_scanner_runs_ran_at on scanner_runs(ran_at desc);
