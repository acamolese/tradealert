-- Tracciamento spesa Anthropic per chiamata. Permette a /status di
-- mostrare la spesa stimata mese per mese cosi' si capisce a colpo
-- d'occhio quando ricaricare i crediti API.
--
-- Il costo e' calcolato lato applicazione a partire da input/output
-- tokens e dalla price table hard-coded in src/llm_usage.py: salviamo
-- gia' il numero pronto qui per evitare ricomputi in lettura.

create table llm_usage (
  id bigserial primary key,
  ran_at timestamptz not null default now(),
  caller text not null,
  model text not null,
  input_tokens int not null default 0,
  output_tokens int not null default 0,
  cache_creation_input_tokens int not null default 0,
  cache_read_input_tokens int not null default 0,
  cost_usd numeric(10, 6) not null default 0
);

create index idx_llm_usage_ran_at on llm_usage(ran_at desc);

alter table llm_usage enable row level security;
