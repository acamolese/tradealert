-- Abilita Row Level Security su tutte le tabelle del progetto.
--
-- Scopo: chiudere l'accesso alle anon/authenticated key che, finora,
-- potevano leggere e modificare qualunque riga. Senza RLS chiunque
-- conosca SUPABASE_URL + anon key (l'anon key e' progettata per essere
-- pubblica) puo' fare CRUD totale.
--
-- Il bot TradeAlert gira server-side e usa la service_role key, che
-- bypassa RLS di default. Quindi non aggiungiamo policy per anon:
-- l'effetto e' "tutto bloccato per anon, tutto aperto per service_role".
--
-- Se in futuro si vorra' esporre una read-only API pubblica (es. per
-- una webapp), aggiungere policy 'select for anon' tabella per tabella.

alter table signals enable row level security;
alter table trades enable row level security;
alter table monitoring_events enable row level security;
alter table account_snapshots enable row level security;
alter table scanner_runs enable row level security;
