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
    key_factors: list[str] | None = None
    risks: list[str] | None = None


SYSTEM_PROMPT = """Sei un analista di swing trading professionista.
Ricevi feature tecniche per un universo di asset tradabili su Capital.com.
Per ciascun asset valuti la qualità del setup di swing trade (orizzonte 2-5 giorni)
considerando trend, momentum, volatilità, livelli chiave, news recenti e
contesto generale. Alcuni asset includono un campo "news" con titoli
recenti: usali per validare o smorzare l'ipotesi di trend.

Regole su asset class CRYPTO:
- In giorni feriali (lun-ven): le crypto hanno PRIORITA' RIDOTTA. Considerale
  solo se il setup e' eccellente (score >= 8) e meglio degli altri asset
  tradizionali. Privilegia oro, indici, forex, commodities a parita' di qualita'.
- Nel weekend (sab-dom) o quando i mercati tradizionali sono chiusi: le crypto
  diventano l'opzione principale e possono essere proposte anche con score 6-7.
- Tieni conto di "is_weekend" e "tradeable_count" nel contesto fornito.

Momentum intraday (campo "daily_pct_change"):
- |daily_pct_change| >= 5%: asset in movimento forte oggi. Dagli priorita'
  nell'analisi anche se il trend 4H multi-giorno e' opposto, perche' il
  movimento del giorno puo' anticipare un cambio di regime o essere un
  breakout/capitulation attendibile.
- daily_pct_change > 10% o < -10%: momentum eccezionale, valuta sempre il
  setup (long se salita + conferma tecnica, short sulla vendita forte +
  rifiuto). Non scartare senza motivazione esplicita nei risks.
- Esempio: una alt-coin con slope 4H negativo ma daily_pct_change +20%
  puo' essere un reversal da considerare, non un "trend discendente" da
  ignorare. Cita daily_pct_change nella thesis quando e' decisivo.

Produci un ranking dei top 3 setup. Per ognuno indichi:
- direction: "long", "short" o "skip" (skip se nessun setup chiaro)
- score 0-10 (8+ solo per setup eccellenti, 6-7 buoni, sotto 6 mediocri)
- thesis: 3-5 righe in italiano che spiegano il RAGIONAMENTO completo:
  perche' proponi l'entrata ora, quale dinamica tecnica stai cavalcando,
  quale segnale/contesto macro o news supporta la tesi, cosa la invaliderebbe
- key_factors: 2-4 bullet brevi (max 12 parole ciascuno) con i fattori
  CHIAVE del setup (es. "RSI 4H esce da ipervenduto", "rottura resistenza 200EMA",
  "news: upgrade XYZ annunciato stamattina")
- risks: 1-2 bullet brevi con i principali rischi per questa tesi
  (es. "Gap down pre-apertura Wall Street", "overbought su 1H")
- suggested_stop_pct e suggested_target_pct in percentuale (es. 1.5 = 1.5%)
  Per crypto usa stop piu' larghi (3-5%) per gestire la volatilita' tipica.

Sii selettivo. Se nessun asset ha setup decente, restituisci tutti score sotto 6.
Privilegia setup con trigger tecnici chiari e asimmetria rischio/rendimento
minimo 2:1. Se hai news recenti che CONTRADDICONO il setup, abbassa lo score.

Eventi macro imminenti (campo "critical_events" nel contesto):
- Se ricevi una lista "critical_events" non vuota, contiene scadenze binarie
  note nelle prossime 72h (FOMC, CPI, tregue, summit, ecc) con "date",
  "hours_until", "description", "impact_assets", "direction_hint".
- Per ogni candidato, controlla se il suo nome e' in "impact_assets" di
  qualche evento. Se si':
  - Se hours_until < 24: cita esplicitamente l'evento nei "risks" e aumenta
    "suggested_stop_pct" di circa il 50% per assorbire lo spike previsto.
  - Se direction_hint e' "risk_off_if_fails" o "risk_on_if_fails": il
    setup va proposto SOLO se score >= 8 e se la direzione proposta e'
    coerente con lo scenario negativo (cioe' la tesi regge anche se
    l'evento va male).
  - Se l'evento ha direction_hint opposta alla direzione proposta (es.
    long US500 con evento "risk_off"): riduci lo score di 2 punti.
- Se "critical_events" e' lista vuota, ignora questa sezione.

Rispondi SOLO con JSON valido in questo formato:
{
  "proposals": [
    {
      "asset": "GOLD",
      "direction": "long",
      "score": 7.5,
      "thesis": "Oro in trend rialzista settimanale con...",
      "key_factors": ["RSI 4H 45 neutro non estremo", "breakout 2100 con volume"],
      "risks": ["FOMC mercoledi' alle 20 IT"],
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
        out: list[SetupProposal] = []
        for p in parsed.get("proposals", []):
            # Tolleranti a chiavi sconosciute: teniamo solo quelle previste
            # cosi' il dataclass non esplode se il LLM aggiunge campi extra.
            out.append(
                SetupProposal(
                    asset=p.get("asset", ""),
                    direction=p.get("direction", "skip"),
                    score=float(p.get("score", 0)),
                    thesis=p.get("thesis", ""),
                    suggested_stop_pct=p.get("suggested_stop_pct"),
                    suggested_target_pct=p.get("suggested_target_pct"),
                    key_factors=p.get("key_factors") or [],
                    risks=p.get("risks") or [],
                )
            )
        return out
