"""Carica configurazione da variabili d'ambiente."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


# Sprint 6 A4.2: capitale di rischio e cap espressi in percentuale, con i
# valori EUR derivati. Ai default (capitale 100, 20%, 5%) i cap calcolati
# sono bit-identici ai vecchi valori assoluti (20 EUR settimanale, 5 EUR
# per trade). Alzare ACCOUNT_RISK_CAPITAL_EUR scala tutti i cap insieme:
# e' l'unica leva da toccare a ogni step della scala 100->300->1000
# (gate pre-registrato in docs/sprint6-piano-scalata.md, A4.3).
ACCOUNT_RISK_CAPITAL_EUR: float = float(
    os.environ.get("ACCOUNT_RISK_CAPITAL_EUR", "100")
)
WEEKLY_DRAWDOWN_CAP_PCT: float = float(
    os.environ.get("WEEKLY_DRAWDOWN_CAP_PCT", "20")
)

# Cap settimanale di drawdown realizzato (valore assoluto in EUR, derivato).
# Se la somma del pnl dei trade chiusi negli ultimi 7 giorni rolling
# scende a -WEEKLY_DRAWDOWN_CAP_EUR o sotto, lo scanner si auto-stoppa.
WEEKLY_DRAWDOWN_CAP_EUR: float = (
    ACCOUNT_RISK_CAPITAL_EUR * WEEKLY_DRAWDOWN_CAP_PCT / 100.0
)


# Sprint 2 Fase 3: kill switch direzionale.
# Misura la CAPACITA' del sistema di vedere gli short, non le condizioni
# di mercato: conta i signal con direction='short' GENERATI da SPRINT2_START
# in poi, a prescindere da esecuzione, filtri o cancellazioni. Se entro
# SPRINT2_KILL_WINDOW_DAYS giorni di calendario il sistema non ne genera
# nemmeno uno, la diagnosi di bias era incompleta: lo scanner si ferma e
# si torna in Fase 2. In 60 giorni pre-modifiche ne era stato generato 1;
# basta >=1 in 30 giorni post-modifiche per confermare la bidirezionalita'.
# NON conta i long ne' i trade chiusi: il mercato decide quanti short si
# concretizzano, il sistema decide solo se li propone. Costanti modulo
# (no env var) come da vincolo Sprint 1.
# Vedi src/scanner.py::_check_directional_kill_switch.
#
# SPRINT2_START e' l'ISTANTE DEL DEPLOY del codice bidirezionale in
# produzione (git reflog: pull --ff-only del 2026-05-20 18:54:34 UTC),
# non la mezzanotte: i signal generati prima dal codice vecchio
# long-biased non devono contare. Il signal short #85 (Brent, 16:05 UTC,
# pre-deploy) e' percio' escluso: contarlo disarmerebbe il kill switch
# prima ancora che la Fase 3 cominci.
SPRINT2_START: str = "2026-05-20T18:54:34+00:00"
SPRINT2_KILL_WINDOW_DAYS: int = 30

# Refinement (giorno 1 di Fase 3): contare solo i signal short rischia un
# falso positivo. Il dedup 24h e gli altri filtri possono sopprimere short
# di qualita' proposti dallo scanner prima che diventino signal (es. il
# 2026-05-21 lo scanner ha proposto Brent short score 7.0 in 3 run, tutte
# soppresse dal dedup perche' Brent gia' segnalato). Se a fine finestra ci
# sono 0 signal short MA almeno SPRINT2_KILL_DISCARDED_TOLERANCE run hanno
# proposto uno short con score >= soglia poi scartato, il kill NON scatta:
# la bidirezionalita' c'e' a livello scanner, e' il dedup/i filtri ad aver
# soppresso. In quel caso si manda solo un avviso Telegram (pausa
# cautelativa). La soglia 3 e' un minimo di evidenza: 3 scansioni distinte
# che propongono uno short di qualita' bastano a escludere il riemergere
# del bias di generazione.
SPRINT2_KILL_DISCARDED_TOLERANCE: int = 3


@dataclass(frozen=True)
class Config:
    capital_api_key: str
    capital_password: str
    capital_identifier: str
    capital_env: str

    telegram_bot_token: str
    telegram_chat_id: str  # owner chat: riceve bottoni e comandi
    telegram_chat_ids: list[str]  # broadcast list per i messaggi informativi

    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str  # preferito lato server: bypassa RLS

    anthropic_api_key: str
    anthropic_model: str
    anthropic_model_fast: str  # modello cheap per task non critici (briefing)

    finnhub_api_key: str  # opzionale, "" se non configurato

    exposure_budget_eur: float  # Esposizione target in EUR per trade (notional)
    max_open_positions: int
    min_score_threshold: float
    execution_mode: str  # "coach" | "confirm" | "auto"
    confirm_timeout_sec: int  # quanto attendere il click su Telegram
    min_rr_at_entry: float  # R:R minimo (vs livelli originali) per aprire al click
    budget_options: list[float]  # preset EUR per bottoni budget Telegram
    trailing_step_r: float  # step in unita' di R per il trailing stop (1.0 conservativo, 0.5 aggressivo)
    max_loss_per_trade_eur: float  # cap perdita massima per singolo trade (EUR)
    scoring_shadow_enabled: bool  # se true, logga score shadow temp0.2 accanto al reale (Sprint 4 troncone 1)
    scoring_two_call: bool  # se true, scoring (Call1 temp0.2, no thesis) + thesis (Call2) separati
    trail_v1_lowband: bool  # se true, rampa V1 nella fascia 0.5-1.0R del trailing (deploy gated, docs/sprint4-trailing-v1-clean.md)
    trail_v2_highband: bool  # se true, rampa V2 nella fascia 1.0-1.25R del trailing (deploy gated, docs/sprint5-trailing-v2-highband.md)
    sizing_currency_aware: bool  # se true, converte il rischio quote->USD nel sizing (bugfix FX, docs/sprint5-sizing-fix.md); OFF = bit-identico
    concentration_block_dup: bool  # se true, NON apre su (asset, direzione) gia' aperto (Sprint 5 tetto B1)
    max_open_per_direction: int  # cap su short/long simultanei; 0 = OFF (Sprint 5 tetto B3)
    concentration_shadow: bool  # se true, logga la regola dinamica "no doppione su tesi che fallisce" (Sprint 5 shadow, LOGGING-ONLY)
    basket_fx_enabled: bool  # se true, aggiunge il primo blocco FX (EUR/USD, AUD/USD, GBP/USD) all'universo di scan (Sprint 5)
    basket_trend_enabled: bool  # se true, aggiunge il blocco trending (Copper, Hang Seng, Nikkei) all'universo di scan (Sprint 5)
    dedup_direction_aware: bool  # se true, il dedup 24h blocca solo (asset, direzione), non l'intero asset (fix: un long dopo uno short chiuso non e' un doppione)
    auto_close_enabled: bool  # se true, su CLOSE del monitor auto-chiude dopo finestra di veto (simmetrico all'auto-confirm apertura)
    auto_close_window_sec: int  # finestra di veto prima dell'auto-chiusura (default 30s)

    @property
    def capital_base_url(self) -> str:
        if self.capital_env == "live":
            return "https://api-capital.backend-capital.com/api/v1"
        return "https://demo-api-capital.backend-capital.com/api/v1"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variabile d'ambiente mancante: {name}")
    return value


def _parse_chat_ids(raw: str) -> list[str]:
    """Parse una lista CSV di chat_id Telegram. Ritorna lista pulita
    (stringhe, valori vuoti scartati)."""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_budget_options(raw: str) -> list[float]:
    values: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = float(part)
        except ValueError:
            continue
        if v > 0:
            values.append(v)
    return sorted(set(values)) or [10.0, 15.0, 20.0, 25.0, 30.0]


def _max_loss_per_trade_eur() -> float:
    """Cap perdita per trade: percentuale del capitale (Sprint 6 A4.2),
    con override legacy in EUR assoluti se MAX_LOSS_PER_TRADE_EUR e' settata
    (deprecata: non scala con ACCOUNT_RISK_CAPITAL_EUR)."""
    legacy = os.environ.get("MAX_LOSS_PER_TRADE_EUR")
    if legacy is not None and legacy.strip():
        import logging

        logging.getLogger(__name__).warning(
            "MAX_LOSS_PER_TRADE_EUR e' deprecata: usa MAX_LOSS_PER_TRADE_PCT "
            "(il valore assoluto non scala con ACCOUNT_RISK_CAPITAL_EUR)"
        )
        return float(legacy)
    pct = float(os.environ.get("MAX_LOSS_PER_TRADE_PCT", "5"))
    return ACCOUNT_RISK_CAPITAL_EUR * pct / 100.0


def load_config() -> Config:
    owner_chat_id = _required("TELEGRAM_CHAT_ID")
    raw_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
    chat_ids = _parse_chat_ids(raw_chat_ids) if raw_chat_ids else [owner_chat_id]
    # L'owner deve sempre essere nella lista di broadcast.
    if owner_chat_id not in chat_ids:
        chat_ids = [owner_chat_id] + chat_ids
    return Config(
        capital_api_key=_required("CAPITAL_API_KEY"),
        capital_password=_required("CAPITAL_API_PASSWORD"),
        capital_identifier=_required("CAPITAL_IDENTIFIER"),
        capital_env=os.environ.get("CAPITAL_ENV", "demo"),
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=owner_chat_id,
        telegram_chat_ids=chat_ids,
        supabase_url=_required("SUPABASE_URL"),
        supabase_anon_key=_required("SUPABASE_ANON_KEY"),
        supabase_service_role_key=os.environ.get(
            "SUPABASE_SERVICE_ROLE_KEY", ""
        ),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get(
            "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        ),
        anthropic_model_fast=os.environ.get(
            "ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001"
        ),
        finnhub_api_key=os.environ.get("FINNHUB_API_KEY", ""),
        exposure_budget_eur=float(
            os.environ.get("EXPOSURE_BUDGET_EUR")
            or os.environ.get("MARGIN_BUDGET_EUR", "15")
        ),
        max_open_positions=int(os.environ.get("MAX_OPEN_POSITIONS", "1")),
        min_score_threshold=float(os.environ.get("MIN_SCORE_THRESHOLD", "7")),
        execution_mode=os.environ.get("EXECUTION_MODE", "coach").lower(),
        confirm_timeout_sec=int(os.environ.get("CONFIRM_TIMEOUT_SEC", "240")),
        min_rr_at_entry=float(os.environ.get("MIN_RR_AT_ENTRY", "1.2")),
        budget_options=_parse_budget_options(
            os.environ.get("BUDGET_OPTIONS", "10,15,20,25,30")
        ),
        trailing_step_r=float(os.environ.get("TRAILING_STEP_R", "0.5")),
        max_loss_per_trade_eur=_max_loss_per_trade_eur(),
        scoring_shadow_enabled=os.environ.get(
            "SCORING_SHADOW", "false"
        ).strip().lower()
        == "true",
        scoring_two_call=os.environ.get(
            "SCORING_TWO_CALL", "false"
        ).strip().lower()
        == "true",
        trail_v1_lowband=os.environ.get(
            "TRAIL_V1_LOWBAND", "false"
        ).strip().lower()
        == "true",
        trail_v2_highband=os.environ.get(
            "TRAIL_V2_HIGHBAND", "false"
        ).strip().lower()
        == "true",
        sizing_currency_aware=os.environ.get(
            "SIZING_CURRENCY_AWARE", "false"
        ).strip().lower()
        == "true",
        concentration_block_dup=os.environ.get(
            "CONCENTRATION_BLOCK_DUP", "false"
        ).strip().lower()
        == "true",
        max_open_per_direction=int(
            os.environ.get("MAX_OPEN_PER_DIRECTION", "0")
        ),
        concentration_shadow=os.environ.get(
            "CONCENTRATION_SHADOW", "true"
        ).strip().lower()
        == "true",
        basket_fx_enabled=os.environ.get(
            "BASKET_FX_ENABLED", "false"
        ).strip().lower()
        == "true",
        basket_trend_enabled=os.environ.get(
            "BASKET_TREND_ENABLED", "false"
        ).strip().lower()
        == "true",
        dedup_direction_aware=os.environ.get(
            "DEDUP_DIRECTION_AWARE", "true"
        ).strip().lower()
        == "true",
        auto_close_enabled=os.environ.get(
            "AUTO_CLOSE_ENABLED", "false"
        ).strip().lower()
        == "true",
        auto_close_window_sec=int(
            os.environ.get("AUTO_CLOSE_WINDOW_SEC", "30")
        ),
    )
