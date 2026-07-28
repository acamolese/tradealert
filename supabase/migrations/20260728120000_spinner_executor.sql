-- TradeSpinner — esecutore demo (spec §6.3/§11). Stato delle posizioni aperte
-- dall'esecutore sul conto DEMO, per distinguerle da altre posizioni presenti
-- (es. la US500 di test §12.8) e riconciliare aperture/chiusure tra run.
-- L'esecutore gira SOLO su demo (guard capital_env=demo + endpoint demo-api).

create table if not exists spinner.executor_position (
  id             bigserial primary key,
  epic           text not null,
  side           text not null,             -- long|short
  deal_id        text,                       -- dealId Capital (per la chiusura)
  deal_reference text,
  size           numeric not null,           -- size eseguita (unita' strumento)
  f_exec         numeric,
  open_price     numeric,
  opened_at      timestamptz not null default now(),
  close_price    numeric,
  closed_at      timestamptz,                -- null = ancora aperta
  close_reason   text                        -- exit_not_target|manual|reconcile_gone
);
create index if not exists exec_pos_open_idx
  on spinner.executor_position (epic, side) where closed_at is null;

alter table spinner.executor_position enable row level security;
create policy executor_position_service_all on spinner.executor_position
  for all to service_role using (true) with check (true);

grant all on spinner.executor_position to service_role;
grant usage, select on all sequences in schema spinner to service_role;
