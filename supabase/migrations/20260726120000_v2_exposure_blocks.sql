-- TradeAlert v2 — Exposure Block Controller
-- Spec: docs/tradealert-v2-exposure-blocks.md (§6). Normativo.
--
-- Nuove tabelle per il controller di esposizione a blocchi. Non tocca le tabelle
-- v1 (trades, signals, ...): v2 gira in parallelo dietro flag, il rollback (§12)
-- non droppa queste tabelle (i dati execution_quality/shadow_ledger hanno valore
-- indipendente dalla strategia attiva).
--
-- NB RLS: allineare le policy al pattern di 20260421230000_enable_rls.sql prima di
-- usare in produzione con la anon key. In fondo si abilita RLS con policy per il
-- service role, coerente col resto dello schema.

-- === 6.1 stato quotidiano del controller (una riga per giorno, immutabile) ===
create table if not exists exposure_state (
  id              bigserial primary key,
  as_of_date      date not null unique,
  epic            text not null,
  sigma_hat       numeric not null,       -- volatilita' annualizzata stimata
  scale_raw       numeric not null,
  scale_applied   numeric not null,
  macro_impact    text,                   -- low|medium|high, loggato sempre
  macro_scale     numeric not null,       -- 1.0 se MACRO_SCALE_ENABLED off
  n_max           int not null,
  blocks_target   int not null,
  blocks_current  int not null,
  action          text not null,          -- open|close|hold|halt
  delta           int not null,
  equity_eur      numeric not null,
  real_leverage   numeric not null,
  created_at      timestamptz not null default now()
);

-- === 6.1 ciclo di vita del singolo blocco ===
create table if not exists block (
  id               bigserial primary key,
  epic             text not null,
  deal_id          text unique,           -- id Capital
  opened_at        timestamptz not null,
  open_price       numeric not null,
  size_units       numeric not null,
  margin_eur       numeric not null,
  catastrophe_stop numeric,               -- livello prezzo, non ATR-based (§7.1)
  closed_at        timestamptz,
  close_price      numeric,
  close_reason     text,                  -- controller|catastrophe|kill_switch|manual
  pnl_ccy          numeric,               -- valuta dello strumento
  pnl_eur          numeric,
  financing_eur    numeric,
  opened_by_state  bigint references exposure_state(id)
);
create index if not exists block_open_idx on block (epic) where closed_at is null;

-- === 6.1 / 6.3 benchmark ombra (obbligatorio) ===
create table if not exists shadow_ledger (
  as_of_date      date not null,
  strategy        text not null,          -- 'controller' | 'always_1_block' | 'flat'
  equity_eur      numeric not null,
  exposure_eur    numeric not null,
  daily_return    numeric not null,
  cum_return      numeric not null,
  cost_modeled    numeric not null,
  primary key (as_of_date, strategy)
);

-- === 6.1 qualita' di esecuzione (tara il simulatore) ===
create table if not exists execution_quality (
  id                bigserial primary key,
  deal_id           text not null,
  side              text not null,        -- open|close
  requested_at      timestamptz not null,
  modeled_price     numeric not null,
  actual_price      numeric not null,
  slippage_bps      numeric not null,
  spread_at_fill    numeric,
  financing_modeled numeric,
  financing_charged numeric
);

-- === 6.2 riproducibilita' delle run di simulazione ===
-- Il conteggio delle configurazioni esplorate DEVE essere una query SQL, non
-- memoria: e' l'input del calcolo del tetto del rumore (noise_ceiling).
create table if not exists sim_run (
  id            bigserial primary key,
  config_hash   text not null,
  data_hash     text not null,
  git_sha       text not null,
  created_at    timestamptz not null default now(),
  notes         text
);

-- === 6.4 vincolo su modifiche di produzione ===
create table if not exists experiment (
  id               bigserial primary key,
  question         text not null,
  metric           text not null,
  gate             text not null,
  preregistered_at timestamptz not null,
  git_sha          text not null
);

create table if not exists adversary_review (
  experiment_id    bigint primary key references experiment(id),
  config_count     int not null,
  benchmark_decomp jsonb not null,
  noise_ceiling    numeric not null,
  verdict          text not null,
  reviewed_at      timestamptz not null default now()
);

-- La foreign key su adversary_review rende impossibile un cambio di config di
-- produzione senza revisione avversaria registrata: il vincolo e' nel DB, non nel
-- processo umano (§6.4).
create table if not exists prod_config_change (
  id            bigserial primary key,
  experiment_id bigint not null references adversary_review(experiment_id),
  param         text not null,
  old_value     text,
  new_value     text not null,
  applied_at    timestamptz not null default now()
);

-- === RLS (coerente con enable_rls.sql): abilita e concede al service role ===
alter table exposure_state     enable row level security;
alter table block              enable row level security;
alter table shadow_ledger      enable row level security;
alter table execution_quality  enable row level security;
alter table sim_run            enable row level security;
alter table experiment         enable row level security;
alter table adversary_review   enable row level security;
alter table prod_config_change enable row level security;

do $$
declare t text;
begin
  foreach t in array array['exposure_state','block','shadow_ledger',
    'execution_quality','sim_run','experiment','adversary_review','prod_config_change']
  loop
    execute format(
      'create policy %I_service_all on %I for all to service_role using (true) with check (true);',
      t, t);
  end loop;
end $$;
