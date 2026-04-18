"""Analisi setup multi-asset con Claude.

Riceve un dizionario di feature tecniche per ciascun asset e restituisce
score, direzione e thesis per i top setup. Volutamente JSON-strict per
permettere parsing affidabile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from .config import Config


@dataclass
class SetupProposal:
    asset: str
    direction: str  # "long" o "short" o "skip"
    score: float    # 0-10
    thesis: str
    suggested_stop_pct: float | None = None
    suggested_target_pct: float | None = None


SYSTEM_PROMPT = """Sei un analista di swing trading professionista.
Ricevi feature tecniche per un universo di asset tradabili su Capital.com.
Per ciascun asset valuti la qualità del setup di swing trade (orizzonte 2-5 giorni)
considerando trend, momentum, volatilità, livelli chiave e contesto generale.

Regole su asset class CRYPTO (BTCUSD, ETHUSD):
- In giorni feriali (lun-ven): le crypto hanno PRIORITA' RIDOTTA. Considerale
  solo se il setup e' eccellente (score >= 8) e meglio di tutti gli altri asset
  tradizionali. Privilegia oro, indici, forex, commodities a parita' di qualita'.
- Nel weekend (sab-dom) o quando i mercati tradizionali sono chiusi: le crypto
  diventano l'opzione principale e possono essere proposte anche con score 6-7.
- Tieni conto del campo "is_weekend" e "tradeable_count" nel contesto fornito.

Produci un ranking dei top 3 setup. Per ognuno indichi:
- direction: "long", "short" o "skip" (skip se nessun setup chiaro)
- score 0-10 (8+ solo per setup eccellenti, 6-7 buoni, sotto 6 mediocri)
- thesis di 2-3 righe in italiano, con focus su: cosa giustifica l'entrata,
  cosa la invaliderebbe, livelli operativi indicativi
- suggested_stop_pct e suggested_target_pct in percentuale (es. 1.5 = 1.5%)
  Per crypto usa stop piu' larghi (3-5%) per gestire la volatilita' tipica.

Sii selettivo. Se nessun asset ha setup decente, restituisci tutti score sotto 6.
Privilegia setup con trigger tecnici chiari e asimmetria rischio/rendimento minimo 2:1.

Rispondi SOLO con JSON valido in questo formato:
{
  "proposals": [
    {
      "asset": "GOLD",
      "direction": "long",
      "score": 7.5,
      "thesis": "...",
      "suggested_stop_pct": 1.2,
      "suggested_target_pct": 2.8
    }
  ]
}
Niente testo prima o dopo il JSON."""


class LLMAnalyzer:
    def __init__(self, config: Config) -> None:
        self._client = Anthropic(api_key=config.anthropic_api_key)
        self._model = config.anthropic_model

    def rank_setups(
        self,
        market_features: dict[str, dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> list[SetupProposal]:
        """market_features: {asset_name: {feature_name: value, ...}}

        context puo' contenere: is_weekend, weekday, tradeable_count,
        traditional_markets_open, ecc.
        """
        payload = {"asset_features": market_features}
        if context:
            payload["context"] = context
        user_message = json.dumps(payload, indent=2, default=str)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text.strip()
        # Difensivo: alcuni modelli aggiungono code fences nonostante il system prompt
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        parsed = json.loads(raw_text)
        return [SetupProposal(**p) for p in parsed.get("proposals", [])]
