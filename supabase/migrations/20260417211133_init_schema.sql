-- TradeAlert MVP schema
-- Quattro tabelle minime per tracciare segnali, trade reali, monitoring, snapshot conto.

create table signals (
  id bigserial primary key,
  created_at timestamptz default now(),
  asset text not null,
  direction text not null check (direction in ('long','short')),
  score numeric not null,
  thesis text,
  entry_price numeric,
  stop_loss numeric,
  take_profit numeric,
  size numeric,
  expected_cost numeric,
  status text not null default 'pending'
    check (status in ('pending','executed','skipped','expired'))
);

create table trades (
  id bigserial primary key,
  signal_id bigint references signals(id),
  capital_deal_id text unique,
  opened_at timestamptz default now(),
  closed_at timestamptz,
  asset text not null,
  direction text not null,
  size numeric not null,
  entry_price numeric not null,
  current_sl numeric,
  current_tp numeric,
  close_price numeric,
  pnl numeric,
  pnl_pct numeric,
  exit_reason text,
  status text not null default 'open'
    check (status in ('open','closed'))
);

create table monitoring_events (
  id bigserial primary key,
  trade_id bigint references trades(id),
  created_at timestamptz default now(),
  event_type text not null,
  reason text,
  details jsonb
);

create table account_snapshots (
  id bigserial primary key,
  taken_at timestamptz default now(),
  balance numeric,
  equity numeric,
  open_positions int,
  daily_pnl numeric
);

create index idx_signals_status on signals(status, created_at desc);
create index idx_trades_status on trades(status, opened_at desc);
create index idx_monitoring_trade on monitoring_events(trade_id, created_at desc);
