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
