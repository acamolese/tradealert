-- Bug #6 Tier 2: tabella ancillare per il link signal->trade
-- scritta PRIMA della chiamata broker.
--
-- Macchina a stati:
--   attempting   -> insert pre create_position
--   capital_open -> create_position OK, deal_id noto, insert_trade non ancora
--                   completato. Da qui il monitor sa il signal_id corretto
--                   se la posizione viene scoperta come orfana.
--   persisted    -> insert_trade riuscito, link gia' presente in trades
--   failed       -> execute_signal ha sollevato eccezione (testo in error_text)
--
-- signal_id e' PK: un signal puo' generare un solo trade nell'orizzonte
-- del bot. Se rilanci una run sullo stesso signal (manuale), upsert.

create table if not exists signal_to_trade_link (
    signal_id bigint primary key references signals(id) on delete cascade,
    attempted_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    status text not null check (status in (
        'attempting',
        'capital_open',
        'persisted',
        'failed'
    )),
    capital_deal_id text,
    error_text text
);

create index if not exists idx_signal_to_trade_link_deal_id
  on signal_to_trade_link(capital_deal_id)
  where capital_deal_id is not null;

create index if not exists idx_signal_to_trade_link_status_attempted
  on signal_to_trade_link(status, attempted_at desc);

-- Trigger di updated_at: ogni update riallinea il campo. Cosi'
-- query "link pending da piu' di N min" sono affidabili.
create or replace function signal_to_trade_link_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_signal_to_trade_link_updated_at on signal_to_trade_link;
create trigger trg_signal_to_trade_link_updated_at
    before update on signal_to_trade_link
    for each row execute function signal_to_trade_link_touch_updated_at();

-- RLS: come per le altre tabelle, niente policy esplicita. service_role
-- bypassa RLS di default; anon non puo' leggere/scrivere. La tabella e'
-- privata e nessun client diretto la consulta.
alter table signal_to_trade_link enable row level security;
