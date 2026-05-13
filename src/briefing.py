"""Briefing tre-volte-al-giorno con news + snapshot mercati + sintesi LLM.

Flow:
1. Login Capital, raccoglie ultime 24h per ogni asset dell'universo
   (variazione %, range, volatilita').
2. Fetch news: RSS pubblici + Finnhub se configurato.
3. Manda tutto a Claude con prompt dedicato per produrre un briefing
   sintetico in italiano (3-5 paragrafi).
4. Invia su Telegram.

Lo slot orario (mattina/pomeriggio/sera) viene passato dal job per
contestualizzare la generazione.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from anthropic import Anthropic

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .llm_usage import log_usage
from .news import fetch_news, format_news_for_llm
from .news_analyzer import analyze_news
from .quiet_hours import is_quiet_now, quiet_reason
from .telegram_client import TelegramClient
from .universe import UNIVERSE

# Capital rate-limit: ~10 req/s sul /markets/{epic}. Con 0.15s tra chiamate
# restiamo ben sotto. Se aggiungiamo asset, tenere sotto soglia.
MARKET_REQUEST_DELAY_SEC = 0.15

log = logging.getLogger(__name__)

SLOT_LABEL = {
    "morning": ("🌅 Briefing mattina", "Mercati EU in apertura, USA chiusi."),
    "afternoon": (
        "🌇 Briefing pomeriggio",
        "Mercati USA in apertura, EU verso chiusura.",
    ),
    "evening": ("🌙 Briefing sera", "Sessione USA in chiusura, focus su domani."),
}


SYSTEM_PROMPT = """Sei un analista di mercato che produce un briefing operativo
in italiano per un trader retail con micro-capitale (15-300 EUR) che opera
swing 2-5 giorni su CFD Capital.com. L'universo e' multi-asset: indici,
forex, commodities, metalli, azioni USA large cap e crypto major (BTC, ETH,
SOL, XRP, ADA, AVAX, DOT, LINK, DOGE e simili sono tradeable sul suo
account Capital.com). Il payload 'market_snapshot' che ricevi contiene
SOLO asset effettivamente tradeable in questo momento: fidati di quello
e NON dire mai che un asset nel payload e' 'fuori scope' o 'non
disponibile'. Se l'ora rende chiusi i mercati tradizionali, e' normale
ricevere solo crypto: produci comunque il briefing.

Il briefing deve:
- essere conciso ma denso, max 5 paragrafi
- citare 1-2 news rilevanti che possono muovere mercati nelle prossime ore,
  privilegiando quelle con 'relevance' >= 7 e 'assets' che compaiono nello snapshot
- segnalare se ci sono eventi macro previsti (FOMC, CPI, ECB, NFP)
- commentare i movimenti significativi degli asset nello snapshot
- chiudere con 1-3 watchlist specifici (asset + livello chiave + perche')
- evitare hype, frasi tipo "il mercato e' in fermento", e claim non supportati
- usare un tono pratico, da analista che parla a un suo collega
- IGNORARE news con category='noise' o relevance < 4: sono rumore gia' filtrato

Format output: testo per Telegram con HTML LIMITATO. Tag ammessi SOLO:
<b>, <i>, <code>. Per andare a capo usa newline (\n), MAI <br>, MAI <p>,
MAI tag non in elenco. Niente preamboli tipo "Ecco il briefing:". Vai dritto
al punto."""


def _capital_snapshot(capital: CapitalClient) -> dict[str, Any]:
    """Per ogni asset universo, calcola variazione 24h e prezzo attuale.
    Throttle tra chiamate per restare sotto il rate limit Capital."""
    snap: dict[str, dict[str, Any]] = {}
    for asset in UNIVERSE:
        try:
            market = capital.get_market(asset.epic)
            s = market.get("snapshot", {})
            if s.get("marketStatus") not in ("TRADEABLE", "EDITS_ONLY"):
                time.sleep(MARKET_REQUEST_DELAY_SEC)
                continue
            bid = s.get("bid")
            offer = s.get("offer")
            high = s.get("high")
            low = s.get("low")
            net_change = s.get("netChange")
            pct_change = s.get("percentageChange")
            mid = (bid + offer) / 2 if bid and offer else None
            snap[asset.name] = {
                "epic": asset.epic,
                "asset_class": asset.asset_class,
                "price": mid,
                "high_24h": high,
                "low_24h": low,
                "net_change": net_change,
                "pct_change_24h": pct_change,
            }
        except CapitalAPIError as exc:
            log.warning("Skip %s nel briefing: %s", asset.name, exc)
        except Exception as exc:
            log.warning("Errore briefing su %s: %s", asset.name, exc)
        time.sleep(MARKET_REQUEST_DELAY_SEC)
    return snap


def generate_briefing(config: Config, slot: str) -> str:
    capital = CapitalClient(config)
    capital.login()

    market_snapshot = _capital_snapshot(capital)
    news_raw = fetch_news(config, limit=25)
    universe_names = [a.name for a in UNIVERSE]
    news_enriched = analyze_news(config, news_raw, universe_names)

    # Filtra via il noise e prioritizza per relevance. Se tutto viene
    # classificato noise (fallback safe) teniamo almeno le prime 12 news
    # per non svuotare il payload al briefing.
    scored = [n for n in news_enriched if n.get("category") != "noise" and n.get("relevance", 0) >= 4]
    scored.sort(key=lambda n: n.get("relevance", 0), reverse=True)
    top_news = scored[:12] if scored else news_enriched[:12]

    client = Anthropic(api_key=config.anthropic_api_key)
    user_block = json.dumps(
        {
            "slot": slot,
            "market_snapshot": market_snapshot,
            "news_recent": [
                {
                    "source": n["source"],
                    "headline": n["headline"],
                    # Tronca i summary RSS (spesso lunghi 500+ char) a 150
                    # per ridurre i token in input. Le headline + 150 char
                    # bastano al LLM per capire il contesto.
                    "summary": (n.get("summary") or "")[:150],
                    "datetime": n.get("datetime"),
                    "assets": n.get("assets", []),
                    "category": n.get("category", ""),
                    "sentiment": n.get("sentiment", ""),
                    "relevance": n.get("relevance", 0),
                }
                for n in top_news
            ],
        },
        indent=2,
        default=str,
    )

    response = client.messages.create(
        model=config.anthropic_model_fast,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_block}],
    )
    log_usage(config, "briefing", response)
    return response.content[0].text.strip()


def run_briefing(config: Config, slot: str) -> None:
    if is_quiet_now():
        log.info("Skip briefing: %s", quiet_reason())
        return

    title, hint = SLOT_LABEL.get(
        slot, ("📰 Briefing", "Aggiornamento generale.")
    )
    telegram = TelegramClient(config)
    try:
        body = generate_briefing(config, slot)
    except Exception as exc:
        log.exception("Briefing fallito")
        telegram.send_message(
            f"🚨 <b>{title} fallito</b>\n<pre>{exc}</pre>"
        )
        return

    msg = (
        f"<b>{title}</b>\n<i>{hint}</i>\n\n{body}"
    )
    # Telegram limita i messaggi a 4096 caratteri
    if len(msg) > 4000:
        msg = msg[:3990] + "\n\n[…]"
    telegram.send_message(msg)
