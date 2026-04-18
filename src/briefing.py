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
from typing import Any

from anthropic import Anthropic

from .capital_client import CapitalAPIError, CapitalClient
from .config import Config
from .news import fetch_news, format_news_for_llm
from .quiet_hours import is_quiet_now, quiet_reason
from .telegram_client import TelegramClient
from .universe import UNIVERSE

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
- citare 1-2 news rilevanti che possono muovere mercati nelle prossime ore
- segnalare se ci sono eventi macro previsti (FOMC, CPI, ECB, NFP)
- commentare i movimenti significativi degli asset nello snapshot
- chiudere con 1-3 watchlist specifici (asset + livello chiave + perche')
- evitare hype, frasi tipo "il mercato e' in fermento", e claim non supportati
- usare un tono pratico, da analista che parla a un suo collega

Format output: testo per Telegram con HTML LIMITATO. Tag ammessi SOLO:
<b>, <i>, <code>. Per andare a capo usa newline (\n), MAI <br>, MAI <p>,
MAI tag non in elenco. Niente preamboli tipo "Ecco il briefing:". Vai dritto
al punto."""


def _capital_snapshot(capital: CapitalClient) -> dict[str, Any]:
    """Per ogni asset universo, calcola variazione 24h e prezzo attuale."""
    snap: dict[str, dict[str, Any]] = {}
    for asset in UNIVERSE:
        try:
            market = capital.get_market(asset.epic)
            s = market.get("snapshot", {})
            if s.get("marketStatus") not in ("TRADEABLE", "EDITS_ONLY"):
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
    return snap


def generate_briefing(config: Config, slot: str) -> str:
    capital = CapitalClient(config)
    capital.login()

    market_snapshot = _capital_snapshot(capital)
    news = fetch_news(config, limit=20)

    client = Anthropic(api_key=config.anthropic_api_key)
    user_block = json.dumps(
        {
            "slot": slot,
            "market_snapshot": market_snapshot,
            "news_recent": [
                {
                    "source": n["source"],
                    "headline": n["headline"],
                    "summary": n.get("summary", ""),
                    "datetime": n.get("datetime"),
                }
                for n in news
            ],
        },
        indent=2,
        default=str,
    )

    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_block}],
    )
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
