-- Salva l'epic Capital.com direttamente sul signal, cosi' l'executor
-- non deve rilookuppare per asset_name (fallisce per asset discovery
-- dinamica che non sono in UNIVERSE/DISCOVERY_WATCHLIST).

alter table signals add column if not exists epic text;
