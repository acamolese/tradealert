"""Arricchisce le news RSS/Finnhub con metadati strutturati via Haiku.

Input: lista news da ``fetch_news`` (headline + summary grezzi).
Output: stessa lista con campi aggiunti:
    - assets: lista di nomi asset dall'UNIVERSE citati/impattati
    - category: macro|geopolitics|earnings|crypto|commodity|fx|noise
    - sentiment: bullish|bearish|neutral (dal punto di vista dell'asset)
    - relevance: 0-10 (importanza per trader swing multi-asset)
    - rationale: motivazione breve (max 80 char)

Filosofia:
- Una singola chiamata batch a Haiku, temperatura 0, JSON strict.
- Fallback safe: se la chiamata fallisce o il JSON non matcha le news
  originali, ritorna la lista input invariata. Il briefing degrada
  al comportamento pre-arricchimento.
- Le news sono identificate dall'indice posizionale nel payload,
  cosi' il LLM non deve riecheggiare headline lunghe (taglio token).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from anthropic import Anthropic

from .config import Config
from .llm_usage import log_usage

log = logging.getLogger(__name__)


_ALLOWED_CATEGORIES = {
    "macro",
    "geopolitics",
    "earnings",
    "crypto",
    "commodity",
    "fx",
    "equity",
    "noise",
}

_ALLOWED_SENTIMENTS = {"bullish", "bearish", "neutral"}


def _system_prompt(universe_names: list[str]) -> str:
    universe_json = json.dumps(universe_names, ensure_ascii=False)
    return f"""Sei un analista finanziario che pre-processa news per un briefing
di trading. Ricevi una lista di news ('items' indicizzato da 0) e devi
produrre un array di oggetti con gli stessi indici arricchiti.

Per ogni news produci:
- idx: l'indice intero della news in input (OBBLIGATORIO, coerente).
- assets: lista di nomi asset impattati, SOLO da questa whitelist:
  {universe_json}
  Scegli solo asset direttamente citati o logicamente impattati
  (es. una news sulla Fed impatta US500/Nasdaq/EUR/USD). Se nessun
  asset della whitelist e' coinvolto, lista vuota [].
- category: una fra {sorted(_ALLOWED_CATEGORIES)}.
  * macro: dati/decisioni banche centrali, CPI, NFP, PIL, FOMC.
  * geopolitics: guerre, sanzioni, summit, elezioni, dazi.
  * earnings: trimestrali, guidance aziendale.
  * crypto: eventi crypto-specifici (ETF, hack, upgrade chain).
  * commodity: oil, gas, oro, metalli industriali.
  * fx: mosse valutarie specifiche.
  * equity: singoli titoli non-earnings (M&A, downgrade rating).
  * noise: gossip, opinion pieces, news di colore, roba datata, clickbait.
- sentiment: bullish|bearish|neutral dal punto di vista degli 'assets'
  elencati. Se assets=[], usa 'neutral'.
- relevance: intero 0-10. Criteri:
  * 9-10: evento market-moving imminente (FOMC oggi, guerra scalation, CPI surprise).
  * 7-8: dato macro importante, earnings big tech, geopolitica rilevante.
  * 5-6: news degna di nota ma non urgente.
  * 3-4: contesto generico, poco azionabile.
  * 0-2: noise, opinion, gossip.
- rationale: max 80 caratteri, italiano, spiega perche' quel relevance.

Output JSON ESATTO, niente prosa fuori, niente code fence:
{{"analyzed": [{{"idx": 0, "assets": [...], "category": "...", "sentiment": "...", "relevance": 0, "rationale": "..."}}]}}

Regole dure:
- L'array 'analyzed' deve contenere UN oggetto per OGNI news in input,
  stesso ordine e stessi idx.
- Non inventare asset fuori whitelist.
- Non usare categorie/sentiment fuori dalle liste consentite.
- Italiano per 'rationale', tutto il resto in inglese/codice.
"""


def _extract_first_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            return obj
        except json.JSONDecodeError:
            continue
    return None


def _validate_entry(
    entry: Any, n_items: int, universe_set: set[str]
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    idx = entry.get("idx")
    if not isinstance(idx, int) or idx < 0 or idx >= n_items:
        return None
    assets_raw = entry.get("assets") or []
    if not isinstance(assets_raw, list):
        assets_raw = []
    assets = [a for a in assets_raw if isinstance(a, str) and a in universe_set]
    category = entry.get("category")
    if category not in _ALLOWED_CATEGORIES:
        category = "noise"
    sentiment = entry.get("sentiment")
    if sentiment not in _ALLOWED_SENTIMENTS:
        sentiment = "neutral"
    try:
        relevance = int(entry.get("relevance", 0))
    except (TypeError, ValueError):
        relevance = 0
    relevance = max(0, min(10, relevance))
    rationale = str(entry.get("rationale") or "").strip()[:120]
    return {
        "idx": idx,
        "assets": assets,
        "category": category,
        "sentiment": sentiment,
        "relevance": relevance,
        "rationale": rationale,
    }


def analyze_news(
    config: Config,
    news: list[dict[str, Any]],
    universe_names: list[str],
) -> list[dict[str, Any]]:
    """Ritorna la lista news con metadati aggiunti. In caso di errore
    ritorna la lista originale (con campi placeholder) per non rompere
    il briefing chiamante."""
    if not news:
        return []

    universe_set = set(universe_names)
    # Payload compatto: solo campi utili al modello, per ridurre token.
    condensed = [
        {
            "idx": i,
            "source": n.get("source", ""),
            "headline": (n.get("headline") or "")[:200],
            "summary": (n.get("summary") or "")[:250],
            "datetime": n.get("datetime", ""),
        }
        for i, n in enumerate(news)
    ]
    user_block = json.dumps({"items": condensed}, ensure_ascii=False)

    try:
        client = Anthropic(api_key=config.anthropic_api_key)
        response = client.messages.create(
            model=config.anthropic_model_fast,
            max_tokens=3000,
            temperature=0,
            system=_system_prompt(universe_names),
            messages=[{"role": "user", "content": user_block}],
        )
        log_usage(config, "news", response)
        raw_text = response.content[0].text.strip()
    except Exception as exc:
        log.warning("analyze_news: chiamata LLM fallita (%s)", exc)
        return _fallback(news)

    raw_text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw_text).strip()
    data = _extract_first_json(raw_text)
    if not isinstance(data, dict):
        log.warning(
            "analyze_news: nessun JSON valido in risposta:\n%s", raw_text[:400]
        )
        return _fallback(news)

    analyzed_raw = data.get("analyzed") or []
    if not isinstance(analyzed_raw, list):
        return _fallback(news)

    by_idx: dict[int, dict[str, Any]] = {}
    for entry in analyzed_raw:
        v = _validate_entry(entry, len(news), universe_set)
        if v is None:
            continue
        by_idx[v["idx"]] = v

    enriched: list[dict[str, Any]] = []
    for i, n in enumerate(news):
        meta = by_idx.get(i)
        if meta is None:
            enriched.append(_with_placeholder(n))
        else:
            merged = dict(n)
            merged.update(
                {
                    "assets": meta["assets"],
                    "category": meta["category"],
                    "sentiment": meta["sentiment"],
                    "relevance": meta["relevance"],
                    "rationale": meta["rationale"],
                }
            )
            enriched.append(merged)
    return enriched


def _with_placeholder(n: dict[str, Any]) -> dict[str, Any]:
    out = dict(n)
    out.setdefault("assets", [])
    out.setdefault("category", "noise")
    out.setdefault("sentiment", "neutral")
    out.setdefault("relevance", 0)
    out.setdefault("rationale", "")
    return out


def _fallback(news: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_with_placeholder(n) for n in news]
