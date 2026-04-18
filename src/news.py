"""Fetch news headlines da fonti pubbliche.

Strategia:
- Default: RSS feed pubblici (BBC Business, ForexLive, Reuters, Investing.com).
  Niente API key, niente registrazione, ToS rispettati (RSS pensati per
  consumo programmatico).
- Fallback opzionale: Finnhub se FINNHUB_API_KEY e' impostata
  (utile se in futuro si vuole arricchire con news per singolo ticker).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import requests

from .config import Config

log = logging.getLogger(__name__)


# Feed RSS pubblici di buona qualita' per macro/finanza
RSS_FEEDS = [
    ("ForexLive", "https://www.forexlive.com/feed/news"),
    ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Reuters Business", "https://www.reuters.com/arc/outboundfeeds/rss/category/business/?outputType=xml"),
    ("Investing.com Forex", "https://www.investing.com/rss/news_285.rss"),
    ("Investing.com Commodities", "https://www.investing.com/rss/news_11.rss"),
    ("Investing.com Stock Markets", "https://www.investing.com/rss/news_25.rss"),
    # --- Crypto-specific ---
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]


# Keywords per il matching news <-> asset. Per un asset non mappato
# usiamo il ``name`` come fallback (minuscolo).
ASSET_KEYWORDS: dict[str, list[str]] = {
    "Bitcoin": ["bitcoin", "btc"],
    "Ethereum": ["ethereum", " eth "],
    "Solana": ["solana", " sol "],
    "Ripple": ["ripple", "xrp"],
    "Cardano": ["cardano", " ada "],
    "Avalanche": ["avalanche", "avax"],
    "Polkadot": ["polkadot", " dot "],
    "Chainlink": ["chainlink"],
    "Dogecoin": ["dogecoin", "doge"],
    "Ethereum Classic": ["ethereum classic", " etc "],
    "EthereumFi": ["ether.fi", "ethfi"],
    "EthereumPoW": ["ethereumpow", "ethw"],
    "ARPA": ["arpa"],
    "Gold": ["gold", "xau"],
    "Silver": ["silver", "xag"],
    "WTI Oil": ["wti", "crude oil", "oil price"],
    "Brent Oil": ["brent", "crude oil", "oil price"],
    "Natural Gas": ["natural gas"],
    "US500": ["s&p 500", "sp500", "s&p500"],
    "Nasdaq 100": ["nasdaq"],
    "DAX 40": ["dax"],
    "FTSE 100": ["ftse"],
    "Nikkei 225": ["nikkei"],
    "EUR/USD": ["eur/usd", "euro dollar", "eurusd"],
    "GBP/USD": ["gbp/usd", "pound", "cable", "sterling"],
    "USD/JPY": ["usd/jpy", "yen"],
    "AUD/USD": ["aud/usd", "aussie"],
    "USD/CHF": ["usd/chf", "swiss franc"],
    "Apple": ["apple", "aapl"],
    "Microsoft": ["microsoft", "msft"],
    "Nvidia": ["nvidia", "nvda"],
    "Tesla": ["tesla", "tsla"],
    "Alphabet": ["alphabet", "google", "googl"],
    "Amazon": ["amazon", "amzn"],
    "Meta": ["meta platforms", " meta "],
    "AMD": [" amd "],
    "Netflix": ["netflix", "nflx"],
    "JPMorgan": ["jpmorgan", "jpm "],
    "Coinbase": ["coinbase", "coin "],
    "Palantir": ["palantir", "pltr"],
    "Super Micro": ["super micro", "smci"],
    "Dave & Buster's": ["dave & buster", "play "],
}


def _entry_dt(entry: Any) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        ts = getattr(entry, field, None) or entry.get(field) if isinstance(
            entry, dict
        ) else getattr(entry, field, None)
        if ts:
            try:
                return datetime(*ts[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_rss_news(
    max_per_feed: int = 5, max_age_hours: int = 18
) -> list[dict[str, Any]]:
    """Aggrega gli ultimi titoli da diversi RSS pubblici.

    Tollerante ai fallimenti: se un feed e' down, va avanti con gli altri.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items: list[dict[str, Any]] = []

    for source, url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(url, request_headers={
                "User-Agent": "tradealert-mvp/0.1 (+https://github.com/acamolese/tradealert)"
            })
        except Exception as exc:
            log.warning("RSS %s fallito: %s", source, exc)
            continue

        for entry in parsed.entries[:max_per_feed]:
            dt = _entry_dt(entry)
            if dt and dt < cutoff:
                continue
            items.append(
                {
                    "source": source,
                    "headline": getattr(entry, "title", "") or "",
                    "summary": (getattr(entry, "summary", "") or "")[:300],
                    "datetime": dt.isoformat() if dt else None,
                    "ts": dt.timestamp() if dt else 0,
                }
            )

    items.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return items


def fetch_finnhub_news(config: Config, limit: int = 15) -> list[dict[str, Any]]:
    """News generali da Finnhub (qualita' alta, formato pulito)."""
    if not config.finnhub_api_key:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": config.finnhub_api_key},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Finnhub fallita: %s", exc)
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=18)).timestamp()
    raw = r.json()
    out = []
    for item in raw:
        ts = item.get("datetime") or 0
        if ts < cutoff:
            continue
        out.append(
            {
                "source": f"Finnhub:{item.get('source', '?')}",
                "headline": item.get("headline") or "",
                "summary": (item.get("summary") or "")[:300],
                "datetime": datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat(),
                "ts": ts,
            }
        )
    out.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return out[:limit]


def _dedup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rimuove duplicati per headline (case-insensitive, primi 60 char)."""
    seen: set[str] = set()
    result = []
    for it in items:
        key = (it.get("headline") or "").lower().strip()[:60]
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result


def fetch_news(config: Config, limit: int = 25) -> list[dict[str, Any]]:
    """Punto di ingresso unico: aggrega RSS pubblici + Finnhub (se key),
    deduplica per headline, ordina per data, ritorna fino a `limit`.
    """
    rss = fetch_rss_news()
    finnhub = fetch_finnhub_news(config)
    merged = _dedup(rss + finnhub)
    merged.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return merged[:limit]


def fetch_finnhub_company_news(
    config: Config, symbol: str, days_back: int = 3, limit: int = 5
) -> list[dict[str, Any]]:
    """News specifiche per ticker (azioni US). Finnhub /company-news.
    Nel free tier: limit 60 req/min, data up to 1 anno.
    """
    if not config.finnhub_api_key:
        return []
    from_date = (
        datetime.now(timezone.utc) - timedelta(days=days_back)
    ).strftime("%Y-%m-%d")
    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
                "token": config.finnhub_api_key,
            },
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Finnhub company-news %s fallita: %s", symbol, exc)
        return []

    out: list[dict[str, Any]] = []
    for item in r.json()[:limit]:
        ts = item.get("datetime") or 0
        out.append(
            {
                "source": f"Finnhub:{item.get('source', '?')}",
                "headline": item.get("headline") or "",
                "summary": (item.get("summary") or "")[:300],
                "datetime": datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat()
                if ts
                else None,
                "ts": ts,
            }
        )
    return out


def news_for_asset(
    asset_name: str,
    all_news: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Filtra news dalla pool globale che menzionano uno dei keyword
    associati all'asset. Match case-insensitive su headline+summary.
    """
    keywords = ASSET_KEYWORDS.get(asset_name) or [asset_name.lower()]
    keywords = [k.lower() for k in keywords]
    matches: list[dict[str, Any]] = []
    for n in all_news:
        text = (
            (n.get("headline") or "") + " " + (n.get("summary") or "")
        ).lower()
        if any(k in text for k in keywords):
            matches.append(n)
        if len(matches) >= limit:
            break
    return matches


def format_news_for_llm(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(nessuna news disponibile)"
    lines = []
    for it in items:
        when = it.get("datetime", "")[:16].replace("T", " ")
        headline = it.get("headline") or ""
        source = it.get("source") or ""
        summary = (it.get("summary") or "")[:200]
        lines.append(f"- [{when}] {headline} ({source}): {summary}")
    return "\n".join(lines)
