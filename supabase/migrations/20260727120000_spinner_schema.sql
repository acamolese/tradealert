-- TradeSpinner — Odds Board (spec docs/tradespinner-odds-board.md, §6). Normativo.
-- Schema Postgres dedicato `spinner`, separato dalle tabelle TradeAlert (public).
-- Fase 1 (EXECUTION_TARGET=none): scanner + tabellone informativo, nessun ordine.

create schema if not exists spinner;

-- === 6.1 una riga per strumento-verso per scansione ===
create table if not exists spinner.odds_board (
  id               bigserial primary key,
  scan_date        date not null,
  epic             text not null,
  asset_class      text not null,
  side             text not null,            -- long|short
  price            numeric not null,
  min_notional_eur numeric not null,
  real_leverage    numeric,
  spread_bps       numeric,                  -- EWMA dei campioni
  spread_samples   int not null default 0,
  fin_annual       numeric,                  -- tasso pagato, negativo = ricevuto
  sigma_ann        numeric,
  mu_total         numeric not null,
  net_drift        numeric,                  -- net
  net_adj          numeric,                  -- net - spread_ann
  f_opt            numeric,
  f_exec           numeric,
  g_exec           numeric,
  status           text not null,            -- eligible|below_gmin|not_executable|not_rated|excluded
  unique (scan_date, epic, side)
);
create index if not exists odds_board_scan_idx on spinner.odds_board (scan_date);
create index if not exists odds_board_status_idx on spinner.odds_board (scan_date, status);

-- === 6.2 ogni modifica alle costanti dichiarate ===
-- fk verso public.experiment di v2: senza revisione avversaria registrata l'insert
-- non passa (stesso principio di prod_config_change). RF_REF e' l'eccezione: si
-- logga senza experiment (aggiornamento dato di mercato) -> nullable + nota.
create table if not exists spinner.constants_log (
  id            bigserial primary key,
  name          text not null,
  old_value     text,
  new_value     text not null,
  experiment_id bigint references public.experiment(id),
  note          text,
  changed_at    timestamptz not null default now()
);

-- === 6.3 interfaccia con l'esecutore (unica superficie di contatto) ===
create table if not exists spinner.target_portfolio (
  as_of_date  date not null,
  epic        text not null,
  side        text not null,
  units       numeric not null,             -- multiplo di minDealSize
  f_exec      numeric not null,
  g_exec      numeric not null,
  reason      text not null,                -- enter|hold|exit
  primary key (as_of_date, epic)
);

-- === 11.1 riconciliazione financing demo ===
create table if not exists spinner.demo_reconcile (
  id           bigserial primary key,
  night        date not null,
  epic         text not null,
  fin_modeled  numeric not null,
  fin_charged  numeric not null,
  delta_rel    numeric not null,
  div_modeled  numeric,
  div_charged  numeric,
  note         text,
  unique (night, epic)
);

-- === 7 shadow ledger dello spinner (controller/flat/best_hindsight_single) ===
create table if not exists spinner.shadow_ledger (
  as_of_date   date not null,
  strategy     text not null,               -- spinner|flat|best_hindsight_single
  equity_eur   numeric not null,
  exposure_eur numeric not null,
  daily_return numeric not null,
  cum_return   numeric not null,
  cost_modeled numeric not null,
  primary key (as_of_date, strategy)
);

-- === 6.4 snapshot settimanale (report + criterio di morte) ===
create materialized view if not exists spinner.board_snapshot_weekly as
select
  date_trunc('week', scan_date)::date as week,
  max(scan_date)                       as last_scan,
  count(*) filter (where status = 'eligible' and side is not null) as eligible_rows,
  count(distinct scan_date)            as scans,
  max(g_exec) filter (where status = 'eligible') as best_g
from spinner.odds_board
group by 1;

-- RLS + policy service_role (Fase 1 isolamento leggero; ruolo dedicato prima di demo)
alter table spinner.odds_board      enable row level security;
alter table spinner.constants_log   enable row level security;
alter table spinner.target_portfolio enable row level security;
alter table spinner.demo_reconcile  enable row level security;
alter table spinner.shadow_ledger   enable row level security;

do $$
declare t text;
begin
  foreach t in array array['odds_board','constants_log','target_portfolio',
    'demo_reconcile','shadow_ledger']
  loop
    execute format(
      'create policy %I_service_all on spinner.%I for all to service_role using (true) with check (true);',
      t, t);
  end loop;
end $$;

-- Esponi lo schema alla API PostgREST (per il client supabase-py)
grant usage on schema spinner to anon, authenticated, service_role;
grant all on all tables in schema spinner to service_role;
grant all on all sequences in schema spinner to service_role;
alter default privileges in schema spinner grant all on tables to service_role;
