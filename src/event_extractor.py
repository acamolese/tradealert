"""Estrae eventi critici binari dalle news usando un LLM (Haiku).

Lo scanner giornaliero ``jobs.macro_scan`` chiama ``extract_events``
passando una lista di news recenti; il LLM restituisce eventi con
``date``, ``description``, ``impact_assets``, ``direction_hint`` nello
stesso formato di ``config/critical_events.json``.

Filosofia:
- Temperatura default, prompt prescrittivo per ridurre allucinazioni.
- Scarta eventi senza data certa o con data nel passato / > 72h.
- Deduplica per descrizione normalizzata.
- Ritorna [] su qualsiasi errore (Anthropic down, JSON rotto, parse
  fallito). Il chiamante continua con i soli eventi manuali.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from anthropic import Anthropic

from .config import Config

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Sei un analista geopolitica-macro per un trading bot.
Ricevi una lista di news recenti. Il tuo compito e' estrarre eventi
BINARI IMMINENTI (prossime 72h) che possano muovere i mercati in modo
direzionale, in particolare:
- Scadenze di negoziati, tregue, dazi, sanzioni
- Summit, vertici, riunioni di emergenza (es. G7, OPEC+)
- Decisioni governative note con data (es. voti parlamentari, referendum)
- Eventi geopolitici con trigger preciso (es. deadline ultimatum)

NON includere:
- Dati macro gia' calendarizzati (CPI, NFP, FOMC): il sistema ha un
  feed economico dedicato, non serve duplicarli qui.
- Earnings aziendali.
- News di colore senza effetto binario direzionale.
- Eventi senza data certa (es. "nei prossimi mesi", "entro l'anno").
- Eventi gia' accaduti.

Per ogni evento produci:
- date: ISO-8601 in UTC (es. "2026-04-22T22:00:00Z"). Se hai solo la
  data senza ora precisa, usa 12:00:00Z.
- description: frase breve in italiano (max 80 caratteri).
- impact_assets: lista di nomi asset tra questi:
  ["Gold", "Silver", "WTI Oil", "Brent Oil", "US500", "Nasdaq 100",
   "DAX 40", "EUR/USD", "GBP/USD", "USD/JPY", "Bitcoin", "Ethereum"].
  Scegli 2-5 asset davvero impattati.
- direction_hint: uno di
  "risk_on", "risk_off", "risk_off_if_fails", "risk_on_if_fails",
  "unknown".

Se NON trovi eventi qualificanti, restituisci events: [].

Output JSON esatto, niente testo fuori:
{"events": [{"date": "...", "description": "...", "impact_assets": [...], "direction_hint": "..."}]}
"""


_ALLOWED_ASSETS = {
    "Gold", "Silver", "WTI Oil", "Brent Oil",
    "US500", "Nasdaq 100", "DAX 40",
    "EUR/USD", "GBP/USD", "USD/JPY",
    "Bitcoin", "Ethereum",
}
_ALLOWED_HINTS = {
    "risk_on", "risk_off",
    "risk_off_if_fails", "risk_on_if_fails",
    "unknown",
}


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_desc(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _extract_first_json(text: str) -> Any:
    """Ritorna il primo oggetto JSON parsabile nella stringa, oppure None.
    Attraversa ogni '{' finche' ``json.JSONDecoder.raw_decode`` accetta."""
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


def _validate_event(
    ev: Any, ref_now: datetime, horizon: datetime
) -> dict[str, Any] | None:
    if not isinstance(ev, dict):
        return None
    dt = _parse_date(ev.get("date"))
    if dt is None or dt < ref_now or dt > horizon:
        return None
    desc = str(ev.get("description") or "").strip()
    if not desc or len(desc) > 200:
        return None
    assets_raw = ev.get("impact_assets") or []
    if not isinstance(assets_raw, list):
        return None
    assets = [a for a in assets_raw if a in _ALLOWED_ASSETS]
    if not assets:
        return None
    hint = str(ev.get("direction_hint") or "unknown")
    if hint not in _ALLOWED_HINTS:
        hint = "unknown"
    return {
        "date": dt.isoformat(),
        "description": desc,
        "impact_assets": assets,
        "direction_hint": hint,
    }


def extract_events(
    config: Config,
    news: list[dict[str, Any]],
    hours_ahead: int = 72,
    max_news: int = 40,
) -> list[dict[str, Any]]:
    """Chiama Haiku con le news e ritorna eventi validati. Ritorna []
    se il LLM non risponde o se nessun evento e' qualificante."""
    if not news:
        return []
    # Passiamo solo headline+summary+source per contenere i token.
    condensed = [
        {
            "source": n.get("source", ""),
            "headline": (n.get("headline") or "")[:200],
            "summary": (n.get("summary") or "")[:300],
            "datetime": n.get("datetime", ""),
        }
        for n in news[:max_news]
    ]
    user_block = json.dumps(
        {"now_utc": datetime.now(timezone.utc).isoformat(), "news": condensed},
        ensure_ascii=False,
    )

    try:
        client = Anthropic(api_key=config.anthropic_api_key)
        response = client.messages.create(
            model=config.anthropic_model_fast,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_block}],
        )
        raw_text = response.content[0].text.strip()
    except Exception as exc:
        log.warning("extract_events: chiamata LLM fallita (%s)", exc)
        return []

    # Rimuovi eventuali code-fence markdown e prova a localizzare il primo
    # blocco JSON valido: Haiku a volte aggiunge commenti di reasoning
    # dopo la chiusura della struttura.
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw_text).strip()
    data = _extract_first_json(raw_text)
    if data is None:
        log.warning(
            "extract_events: nessun JSON valido in risposta:\n%s",
            raw_text[:400],
        )
        return []

    ref_now = datetime.now(timezone.utc)
    horizon = ref_now + timedelta(hours=hours_ahead)
    events_raw = data.get("events") or []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in events_raw:
        v = _validate_event(ev, ref_now, horizon)
        if not v:
            continue
        key = _normalize_desc(v["description"])
        if key in seen:
            continue
        seen.add(key)
        v["source"] = "auto"
        out.append(v)
    out.sort(key=lambda e: e["date"])
    return out
