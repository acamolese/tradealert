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
