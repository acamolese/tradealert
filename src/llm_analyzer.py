"""Analisi setup multi-asset con Claude.

Riceve un dizionario di feature tecniche per ciascun asset e restituisce
score, direzione e thesis per i top setup. Volutamente JSON-strict per
permettere parsing affidabile.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from .config import Config
from .llm_usage import log_usage

log = logging.getLogger(__name__)


def _compress_features(
    features: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Arrotonda i float a 2 decimali per ridurre i token in input alla LLM.
    I prezzi assoluti (level_*, last_price) restano a 4 decimali perche'
    forex/metalli hanno precisione necessaria sotto la seconda cifra.
    Non rinomina i campi: il system prompt si aspetta i nomi originali.
    """
    price_keys = {
        "last_price",
        "level_support",
        "level_resistance",
        "bb_upper",
        "bb_lower",
        "bb_middle",
        "ema_20",
        "ema_50",
        "ema_200",
    }
    out: dict[str, dict[str, Any]] = {}
    for asset, af in features.items():
        compact: dict[str, Any] = {}
        for k, v in af.items():
            if isinstance(v, float):
                compact[k] = round(v, 4 if k in price_keys else 2)
            else:
                compact[k] = v
        out[asset] = compact
    return out


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

Feature disponibili per ciascun asset (usa SOLO queste, niente altro):
- last_price (prezzo corrente), rsi_14 (RSI 14 su 4H), trend_slope_pct
  (regressione lineare 20 candele 4H, %, segno indica direzione e magnitudine
  la forza: e' il medio-trend, ~3.3 giorni), trend_slope_short_pct (stessa
  regressione ma su 8 candele 4H, ~1.3 giorni: e' il trend CORTO, reagisce
  prima all'inizio di uno swing quando trend_slope_pct e' ancora in ritardo),
  atr_4h, atr_pct_of_price (ATR in % del prezzo), bb_width_pct
  (ampiezza % delle Bollinger Bands; NON ricevi i valori delle singole
  bande, solo la larghezza), candles_used, high_20, low_20,
  pct_from_high_20 (distanza % dal massimo 20 candele), spread_pct,
  market_status, daily_pct_change, daily_range_pct, pct_from_daily_high,
  asset_class, epic, news (lista di titoli recenti).
Contesto: is_weekend, weekday, traditional_markets_open, tradeable_count,
critical_events, economic_calendar.

VINCOLO STRETTO: NON puoi citare nella thesis o nei key_factors termini
o concetti non derivabili dalle feature sopra. In particolare sono
VIETATI: medie mobili di qualsiasi lunghezza (non le ricevi), riferimenti
alla banda alta/bassa/centrale di Bollinger o al "tocco" di una banda
direzionale (hai solo l'ampiezza in bb_width_pct), livelli numerici di
supporto/resistenza (non hai i valori), volume (non lo ricevi), order
book, open interest. Se devi parlare di "trend" usa trend_slope_pct e trend_slope_short_pct; se
devi parlare di "compressione/espansione di volatilita" usa bb_width_pct
e atr_pct_of_price; se devi parlare di "vicinanza a estremi recenti" usa
pct_from_high_20 o pct_from_daily_high.

Regole su asset class CRYPTO (stessi standard di qualita' in weekday e weekend):
- Le crypto vengono valutate con gli STESSI criteri di qualita' di oro,
  indici, forex e commodities. Score 8+ resta riservato a setup eccellenti,
  score 7 a setup solidi. NON esiste deroga "weekend": uno score 7 deve
  significare la stessa cosa il sabato e il martedi'.
- Nei weekday le crypto restano in secondo piano: a parita' di qualita'
  privilegia oro, indici, forex, commodities (asset class tradizionali con
  liquidita' e narrative piu' robuste).
- Nei weekend l'universo crypto e' ristretto (solo major: BTC, ETH, SOL,
  XRP, ADA, AVAX, DOT, LINK, DOGE) e i mercati tradizionali sono chiusi.
  Per proporre un setup crypto weekend deve soddisfare TUTTI questi
  criteri di qualita' rigorosi:
    * Trend coerente: trend_slope_pct con segno allineato alla direzione
      proposta e magnitudine |trend_slope_pct| >= 0.3 (no controtrend a
      meno di evidente reversal supportato da daily_pct_change e news).
    * RSI non estremo: 4H tra 35 e 70 per setup long, tra 30 e 65 per
      short. Sopra 75 o sotto 25 indica esaurimento, NON continuazione.
    * Volatilita coerente: bb_width_pct in compressione (valori bassi
      relativi all'asset) suggerisce breakout imminente, in espansione
      suggerisce continuazione. Combinare con pct_from_high_20: per long
      privilegia setup vicini al massimo 20 candele (pct_from_high_20
      negativa ma piccola in modulo), per short setup distanti dal
      massimo (pct_from_high_20 marcatamente negativa).
    * daily_pct_change non blow-off: |daily_pct| > 12% e RSI 4H > 70
      e' un esaurimento, non un breakout. Scarta.
    * Catalyst chiaro: una news/event/macro che spiega il movimento, o
      un livello tecnico chiave da rompere/respingere. NO "il momentum
      e' positivo" senza un perche' identificabile.
- Se NESSUN crypto major weekend soddisfa TUTTI i criteri sopra, restituisci
  proposals con direction="skip" o lista vuota. NON forzare un setup mediocre
  solo per riempire l'output: nel weekend non serve sempre proporre qualcosa.
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

Direzione del setup: long E short con pari dignita'.
Il sistema NON e' long-only. Per ogni asset valuti SIA un possibile long SIA un
possibile short e proponi la direzione che la tecnica supporta meglio. In un
universo in downtrend e' corretto e atteso che i top setup siano short: non
forzare un long contrarian solo perche' "il prezzo e' sceso troppo".

Come riconoscere un setup SHORT di qualita' (speculare al setup long):
- trend_slope_pct e/o trend_slope_short_pct negativi (vedi sotto la lettura
  combinata delle due pendenze);
- daily_pct_change negativo, oppure prezzo che rompe al ribasso i riferimenti
  recenti (low_20, minimo giornaliero);
- pct_from_high_20 marcatamente negativo: prezzo gia' staccato dai massimi e in
  discesa. E' lo speculare del long, che invece privilegia pct_from_high_20
  vicino a 0 (test del massimo);
- RSI 4H in calo nella fascia 40-55: e' un downtrend IN CORSO, non un rimbalzo
  imminente. L'entrata short ottimale e' all'INIZIO del movimento, non quando
  l'RSI e' gia' crollato sotto 25;
- bb_width_pct in espansione, coerente con la continuazione ribassista.
Uno short di continuazione NON richiede un RSI gia' in ipervenduto: aspettare
l'ipervenduto significa entrare a movimento quasi concluso.

Lettura combinata delle due pendenze (trend_slope_pct medio, ~3.3gg, e
trend_slope_short_pct corto, ~1.3gg):
- trend_slope_short_pct negativo mentre trend_slope_pct e' ancora piatto o
  positivo: e' l'INIZIO di uno swing-down, la pendenza corta si gira prima
  della media. E' un setup SHORT di continuazione valido a tutti gli effetti:
  NON aspettare che anche la pendenza a 20 candele diventi negativa, a quel
  punto il movimento e' gia' avanzato e lo short rende meno.
- entrambe le pendenze negative: downtrend consolidato, short ad ALTA
  convinzione: a parita' di altri fattori merita uno score piu' alto.
- trend_slope_pct e trend_slope_short_pct entrambi positivi o piatti: uptrend
  in corso, scenario long. Specularmente, trend_slope_short_pct positivo con
  trend_slope_pct piatto o negativo e' l'inizio di uno swing-up (long di
  continuazione).
- Quando le due pendenze sono in conflitto, la CORTA pesa di piu' per il
  timing di ingresso: dice cosa sta facendo il prezzo adesso.

Lettura dell'RSI, simmetrica e con soglie numeriche esplicite:
- Ipervenduto = RSI 4H STRETTAMENTE sotto 25. Ipercomprato = RSI sopra 75.
  RSI a 26, 31, 38, 44 NON e' ipervenduto: non usare le parole "ipervenduto"
  ne' "esaurimento ribassista" per RSI >= 25, e non rifiutare uno short in
  questo range con quella motivazione. Usa SOLO la soglia 25, non soglie
  interne diverse e meno estreme (es. 30 o 35).
- RSI fra 25 e 45 in discesa = debolezza che PUO' CONTINUARE: e' un downtrend
  in corso, scenario di SHORT di continuazione, NON automaticamente una molla
  per un rimbalzo long. Esempio concreto: RSI 31 che scende con
  trend_slope_short_pct negativo e' un breakdown in atto, va lavorato come
  short, non scartato come "ipervenduto". Specularmente RSI fra 55 e 75 in
  salita = forza che puo' continuare, scenario long.
- Solo gli estremi VERI (RSI < 25 o > 75) segnalano possibile esaurimento e
  reversal nella direzione opposta. In quella zona, e solo li', valuta il
  reversal.

Produci un ranking dei top 3 setup. Per ognuno indichi:
- direction: "long", "short" o "skip" (skip se nessun setup chiaro)
- score 0-10 (8+ solo per setup eccellenti, 6-7 buoni, sotto 6 mediocri)
- thesis: 3-5 righe in italiano che spiegano il RAGIONAMENTO completo:
  perche' proponi l'entrata ora, quale dinamica tecnica stai cavalcando,
  quale segnale/contesto macro o news supporta la tesi, cosa la invaliderebbe
- key_factors: 2-4 bullet brevi (max 12 parole ciascuno) con i fattori
  CHIAVE del setup. Esempi long: "RSI 4H esce da ipervenduto", "trend_slope_pct
  positivo +0.6 su 4H", "bb_width_pct in compressione", "pct_from_high_20 -1.2%
  pronto al test", "news: upgrade XYZ annunciato". Esempi short: "trend_slope_short_pct
  -0.4 negativo, swing-down avviato", "RSI 4H 38 in calo, debolezza non ipervenduto",
  "pct_from_high_20 -3% con momentum ribassista", "daily_pct_change -2% conferma
  la pressione in vendita"
- risks: 1-2 bullet brevi con i principali rischi per questa tesi
  (es. "Gap down pre-apertura Wall Street", "overbought su 1H")
- suggested_stop_pct e suggested_target_pct in percentuale (es. 1.5 = 1.5%)
  Per crypto usa stop piu' larghi (3-5%) per gestire la volatilita' tipica.

Sii selettivo. Se nessun asset ha setup decente, restituisci tutti score sotto 6.
Privilegia setup con trigger tecnici chiari e asimmetria rischio/rendimento
minimo 2:1. Se hai news recenti che CONTRADDICONO il setup, abbassa lo score.

Lo standard di selettivita' e il minimo 2:1 di rischio/rendimento si applicano
in modo IDENTICO a long e short. NON ridurre lo score di un setup solo perche'
e' uno short: uno short tecnicamente solido merita lo stesso score di un long
tecnicamente solido equivalente. Non motivare uno score basso con "asimmetria
sfavorevole" o "controtrend" se il setup short ha trigger tecnici chiari
(trend_slope_pct negativo, RSI in calo, pct_from_high_20 negativo) e
suggested_target_pct / suggested_stop_pct >= 2. Uno short di continuazione di
un downtrend non e' "controtrend": e' allineato al trend.

Eventi macro imminenti:
Il contesto puo' includere due liste di eventi nelle prossime 72h.

(A) "critical_events": scadenze binarie curate a mano (summit, tregue,
eventi geopolitici). Ogni voce ha "hours_until", "description",
"impact_assets", "direction_hint" (risk_on/risk_off/risk_off_if_fails/
risk_on_if_fails/unknown).

(B) "economic_calendar": appuntamenti ufficiali high-impact da Finnhub
(FOMC, CPI, NFP, BCE, BoE, rate decisions). Ogni voce ha "hours_until",
"event", "country", "impact", "estimate", "prev". I paesi piu' comuni
mappano cosi' sugli asset:
  - US -> US500, Nasdaq 100, USD/JPY, Gold, Silver, WTI, Brent
  - EU -> DAX 40, EUR/USD
  - GB -> GBP/USD
  - JP -> USD/JPY

Regole unificate per entrambe le liste:
- Se il candidato e' esposto a un evento entro 24h: cita l'evento nei
  "risks" e aumenta "suggested_stop_pct" del 50% circa.
- Se un critical_events ha direction_hint "risk_off_if_fails" o
  "risk_on_if_fails": proponi il setup SOLO con score >= 8 e direzione
  coerente con lo scenario negativo.
- Se critical_events ha direction_hint esplicitamente opposta alla
  direzione proposta (long su US500 con evento "risk_off"): riduci
  lo score di 2 punti.
- Se un evento FOMC/BCE/BoE e' atteso entro 12h e il setup va contro
  la direzione ritenuta probabile dal mercato (usa "estimate" vs "prev"
  come indicazione grezza): riduci lo score di 1 punto.
- Se entrambe le liste sono vuote, ignora queste regole.

Termini vietati nella thesis e nei key_factors (NON usarli mai, non hai
i dati per supportarli):
- Qualsiasi media mobile esponenziale o semplice di qualsiasi lunghezza
  (incluse quelle a 20, 50 o 200 periodi).
- Qualsiasi riferimento alla banda alta, bassa o centrale di Bollinger,
  al "tocco" o al "respingimento" della banda direzionale: hai SOLO
  l'ampiezza percentuale in bb_width_pct.
- Livelli numerici di supporto o resistenza (es. "supporto a 1.0850",
  "resistenza 2100"): non hai i valori dei livelli, hai solo high_20 e
  low_20 come riferimenti relativi. Puoi citare "test del massimo 20
  candele" o "vicino al minimo 20 candele" ma NON inventare numeri.
- Volume, order book, open interest, funding rate: non li ricevi.
Se hai bisogno di esprimere un concetto bandito, riformulalo usando le
feature reali (trend_slope_pct, bb_width_pct, atr_pct_of_price,
pct_from_high_20, rsi_14, daily_pct_change, news).

Rispondi SOLO con JSON valido in questo formato (l'esempio mostra un long e uno
short, a parita' di standard di qualita'):
{
  "proposals": [
    {
      "asset": "GOLD",
      "direction": "long",
      "score": 7.5,
      "thesis": "Oro con trend_slope_pct positivo su 4H e RSI 4H 45 non estremo, bb_width_pct in espansione coerente con continuazione rialzista, news macro favorevoli al risk-off su metalli.",
      "key_factors": ["RSI 4H 45 neutro non estremo", "trend_slope_pct +0.5 coerente long", "bb_width_pct in espansione"],
      "risks": ["FOMC mercoledi' alle 20 IT"],
      "suggested_stop_pct": 1.2,
      "suggested_target_pct": 2.8
    },
    {
      "asset": "Brent Oil",
      "direction": "short",
      "score": 7.5,
      "thesis": "Brent in downtrend su 4H: trend_slope_short_pct -0.5 gia' negativo con trend_slope_pct -0.2 che conferma, RSI 4H a 44 in calo (sopra 25, non ipervenduto), daily_pct_change -2.1% che conferma la pressione in vendita. pct_from_high_20 -3.8% segnala prezzo gia' staccato dai massimi e in discesa, bb_width_pct in espansione coerente con continuazione ribassista. Short di continuazione del downtrend in atto, invalidato da un recupero deciso del massimo 20 candele.",
      "key_factors": ["trend_slope_short_pct -0.5 e trend_slope_pct -0.2, downtrend confermato", "RSI 4H 44 in calo, debolezza non ipervenduto", "daily_pct_change -2.1% conferma vendita", "pct_from_high_20 -3.8% staccato dai massimi"],
      "risks": ["Rimbalzo tecnico se RSI 4H scende sotto 25", "Catalyst macro energetico a sorpresa"],
      "suggested_stop_pct": 1.8,
      "suggested_target_pct": 4.0
    }
  ]
}
Niente testo prima o dopo il JSON."""


class LLMAnalyzer:
    def __init__(self, config: Config) -> None:
        # Beta header per il TTL esteso a 1h sulla prompt cache: il system
        # prompt e' identico ad ogni scan, la cache viene scritta la prima
        # volta e riletta dagli scan successivi a 1/10 del prezzo input.
        # Lo scanner gira ogni ora: 1h TTL e' il match naturale, ogni lettura
        # rinfresca la cache e mantiene il break-even fra write e read.
        self._client = Anthropic(
            api_key=config.anthropic_api_key,
            default_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"},
        )
        self._model = config.anthropic_model
        self._config = config

    def rank_setups(
        self,
        market_features: dict[str, dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> list[SetupProposal]:
        """market_features: {asset_name: {feature_name: value, ...}}

        context puo' contenere: is_weekend, weekday, tradeable_count,
        traditional_markets_open, ecc.
        """
        payload = {"asset_features": _compress_features(market_features)}
        if context:
            payload["context"] = context
        # Serializzazione compatta (no indent) per ridurre ulteriormente
        # i token di input: il modello non ha bisogno del pretty-print.
        user_message = json.dumps(payload, default=str, separators=(",", ":"))

        # Prompt cache 1h sul system prompt (verificato: ~3.5k char =
        # ~5k tokens, sopra il minimo di 1024/2048 per Sonnet/Haiku).
        # La logica del modello e' invariata: il caching e' trasparente
        # alla generazione, riduce solo il costo input dell'~90% sul
        # cached portion (system prompt) dopo la prima scrittura.
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        log_usage(self._config, "scanner", response)

        raw_text = response.content[0].text.strip()
        # Difensivo: alcuni modelli aggiungono code fences nonostante il system prompt
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        # Difensivo: il modello a volte produce JSON valido + testo extra
        # dopo (es. hit di max_tokens su un secondo blocco, oppure
        # markdown/note finali nonostante il "Niente testo prima o dopo").
        # Usa raw_decode per parsare il PRIMO valore JSON valido e ignorare
        # qualunque trailing extra. Compatibile col caso normale.
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            if "Extra data" in str(exc):
                try:
                    parsed, _end = json.JSONDecoder().raw_decode(raw_text)
                    log.warning(
                        "LLM ha output extra data dopo il JSON valido "
                        "(raw_text len=%d, end=%d); parsato solo il primo "
                        "blocco. output_tokens=%s",
                        len(raw_text), _end,
                        getattr(getattr(response, "usage", None),
                                "output_tokens", None),
                    )
                except json.JSONDecodeError:
                    log.error(
                        "LLM JSON parse fallito anche con raw_decode: %s. "
                        "raw_text[:500]=%r", exc, raw_text[:500],
                    )
                    raise
            else:
                log.error(
                    "LLM JSON parse fallito (%s). raw_text[:500]=%r",
                    exc, raw_text[:500],
                )
                raise
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
