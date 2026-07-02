-- Sprint 6 A4.1: persistere il rischio all'apertura e l'R di uscita.
-- risk_at_open_eur: perdita stimata se scatta lo SL iniziale (sizing.risk_estimate,
--   valuta di riferimento USD trattata come EUR: stesso skew ~15% documentato del
--   max_loss cap, vedi docs/sprint5-sizing-fix.md).
-- exit_r: pnl / risk_at_open_eur, calcolato da Database.close_trade alla chiusura.
-- Il codice rileva a runtime la presenza delle colonne (Database.trades_risk_columns_available):
-- prima di questa migration il comportamento resta bit-identico, quindi l'ordine
-- deploy codice -> migration e' sicuro in entrambe le direzioni.
alter table trades add column if not exists risk_at_open_eur numeric;
alter table trades add column if not exists exit_r numeric;

comment on column trades.risk_at_open_eur is 'Perdita stimata a SL iniziale (EUR~USD ref), scritta da executor all''apertura';
comment on column trades.exit_r is 'pnl / risk_at_open_eur, scritto da close_trade alla chiusura';
