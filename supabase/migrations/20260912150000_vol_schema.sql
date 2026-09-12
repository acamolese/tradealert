-- Vendita di assicurazione sulla volatilita' (jobs/paura_esegui.py, dal 2026-09-07).
-- Fino ad ora il sistema non scriveva nulla: lo stato viveva in data/paura.json
-- sulla VM, quindi nessuna decisione era analizzabile a posteriori. Qui si
-- registrano le decisioni (con le feature che le hanno prodotte), le posizioni
-- aperte dal sistema e la misura in ombra delle varianti di esposizione.
-- Gira SOLO sul conto di prova: il guard e' nel codice (capital_env=demo).

create schema if not exists vol;

-- Una riga per ogni run del job, anche quando non fa nulla.
create table if not exists vol.decisione (
  id                    bigserial primary key,
  creato_il             timestamptz not null default now(),
  epic                  text not null,
  gradino               text not null,            -- fermo|ritirata|base|favorevole|pieno
  frazione_target       numeric not null,         -- frazione del capitale dichiarato
  capitale_eur          numeric,
  nozionale_attuale_eur numeric,
  nozionale_target_eur  numeric,
  size_prima            numeric,
  size_dopo             numeric,
  azione                text not null,            -- nessuna|apertura|aumento|riduzione|chiusura|stop|pausa
  eseguito              boolean not null default false,
  motivo                text,
  risultato_eur         numeric,                  -- P&L aperto al momento della decisione
  stop_level            numeric,
  features_at_decision  jsonb                     -- segnale e stato del conto, per le analisi retrospettive
);
create index if not exists vol_decisione_ts_idx on vol.decisione (creato_il desc);
create index if not exists vol_decisione_azione_idx on vol.decisione (azione, creato_il desc);

-- Posizioni aperte dal sistema, per riconciliare con il broker.
create table if not exists vol.posizione (
  id                bigserial primary key,
  epic              text not null,
  deal_id           text,
  verso             text not null,                -- short|long
  size              numeric not null,
  prezzo_apertura   numeric,
  aperta_il         timestamptz not null default now(),
  prezzo_chiusura   numeric,
  chiusa_il         timestamptz,                  -- null = ancora aperta
  risultato_eur     numeric,
  motivo_chiusura   text                          -- stop|ritirata|manuale|sparita_dal_broker
);
create index if not exists vol_posizione_aperte_idx
  on vol.posizione (epic) where chiusa_il is null;

-- Misura in ombra: cosa avrebbero fatto le varianti, senza eseguirle.
-- Serve al gate pre-registrato in docs/gate-esposizione-volatilita.md.
create table if not exists vol.segnale_shadow (
  id                 bigserial primary key,
  giorno             date not null unique,
  creato_il          timestamptz not null default now(),
  vix                numeric,
  vixm               numeric,
  pendenza           numeric,                     -- VIXM/VIX, sopra 1 = contango
  percentile_vix     numeric,
  prezzo_epic        numeric,
  frazione_fissa     numeric,                     -- baseline: sempre al gradino base
  frazione_scala     numeric,                     -- quello che il sistema esegue
  frazione_spinta    numeric,                     -- variante piu' aggressiva, mai eseguita
  gradino_scala      text,
  gradino_spinta     text,
  features           jsonb
);

alter table vol.decisione enable row level security;
alter table vol.posizione enable row level security;
alter table vol.segnale_shadow enable row level security;

create policy vol_decisione_service_all on vol.decisione
  for all to service_role using (true) with check (true);
create policy vol_posizione_service_all on vol.posizione
  for all to service_role using (true) with check (true);
create policy vol_segnale_shadow_service_all on vol.segnale_shadow
  for all to service_role using (true) with check (true);

grant usage on schema vol to service_role;
grant all on all tables in schema vol to service_role;
grant usage, select on all sequences in schema vol to service_role;
