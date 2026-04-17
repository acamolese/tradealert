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
]


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
