# TradeAlert

Sistema MVP di intelligence di mercato e gestione swing trade su Capital.com.
Scansiona un universo multi-asset (oro, indici, forex, commodities), genera
segnali con scoring LLM, propone trade preformattati via Telegram. In fase Coach
l'esecuzione è manuale, in fase 2 diventa semi-automatica con conferma, in
fase 3 full-auto con limiti di rischio hard.

## Stack
- **GitHub Actions** per i job schedulati (cron analisi e monitoring)
- **Supabase** (PostgreSQL) per storico segnali, trade, eventi di monitoring
- **Anthropic Claude** (Haiku) per scoring setup e generazione thesis
- **Capital.com API** per dati di mercato e (in futuro) esecuzione ordini
- **Telegram Bot API** per notifiche

## Struttura
```
src/                # moduli applicativi
jobs/               # entry point eseguiti dai workflow
supabase/           # config CLI + migration SQL versionate
.github/workflows/  # cron GitHub Actions
```

## Sprint 1 (2026-04-25): configurazione cristallizzata

Fix applicati per portare il sistema in stato osservabile e auto-difensivo
durante la fase Coach:

- **Fix 1.1** prompt LLM allineato al payload reale (no piu' riferimenti a
  EMA20/50/200, banda upper/lower Bollinger, livelli numerici S/R, volume).
- **Fix 1.2** cap settimanale drawdown realizzato a -20 EUR (rolling 7gg
  sui trade chiusi). Scanner si auto-stoppa, notifica Telegram una sola
  volta per giorno solare UTC, run tracciata in `scanner_runs.outcome='risk_cap'`.
  Costante: `WEEKLY_DRAWDOWN_CAP_EUR=20.0` in `src/config.py` (no env var).
- **Fix 1.3** universo ridotto a 5 asset core (Gold, Brent Oil, US500,
  Nasdaq 100, Bitcoin). Discovery dinamica disattivata via
  `ENABLE_DISCOVERY=False` in `src/scanner.py`. Allowlist weekend ridotta
  a {Bitcoin}.
- **Fix 1.4** colonna `signals.features_at_decision JSONB` per persistere
  il contesto tecnico di ogni decisione LLM (RSI, ATR, slope, BB width,
  pct_from_high, daily_pct, news). Abilita analisi retrospettive e backtest.

Parametri di rischio (parametri immutabili dello Sprint 1):

- Capitale rischio totale: 100 EUR (kill switch finale, fuori scope Sprint 1).
- Cap settimanale drawdown: 20 EUR (Fix 1.2).
- Universo: 5 asset core (Fix 1.3).
- Sample target prima di ricalibrare soglie: 30-50 trade chiusi.

## Setup locale (dev)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# completa .env con i tuoi valori
```

## Deploy
Le credenziali in produzione sono caricate da GitHub Secrets, non da `.env`.
I workflow in `.github/workflows/` leggono direttamente dai secrets configurati
nel repository (`Settings -> Secrets and variables -> Actions`).

## Briefing 3x/giorno (DISABILITATO)
Dal 2026-04-24 i briefing automatici (mattina/pomeriggio/sera) sono disabilitati
per contenere i costi API Anthropic. La disabilitazione è su due livelli:

1. `deploy/crontab.txt`: le 3 righe `python -m jobs.briefing ...` sono commentate.
2. `jobs/briefing.py`: guard `BRIEFING_ENABLED`; se non è `true` l'entrypoint
   esce con codice 0 senza chiamare Capital, news o Claude.

Disabilitando il briefing si ferma automaticamente anche la classificazione LLM
delle news (`src/news_analyzer.py::analyze_news` è chiamata solo dal briefing).
Scanner e position monitor non sono impattati: usano `fetch_news` sugli RSS
grezzi, senza classificazione.

### Riabilitare
Sulla VM Oracle:
```bash
# 1. imposta la env nel .env di produzione
echo "BRIEFING_ENABLED=true" >> /home/ubuntu/tradealert/.env

# 2. scommenta le 3 righe briefing in deploy/crontab.txt, poi
crontab /home/ubuntu/tradealert/deploy/crontab.txt
crontab -l   # verifica
```
