"""Carica configurazione da variabili d'ambiente."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


# Cap settimanale di drawdown realizzato (valore assoluto in EUR).
# Se la somma del pnl dei trade chiusi negli ultimi 7 giorni rolling
# scende a -WEEKLY_DRAWDOWN_CAP_EUR o sotto, lo scanner si auto-stoppa.
# Costante modulo (no env var) come da vincolo Sprint 1.
# Capitale rischio totale 100 EUR; cap 20 EUR e' il 20% di drawdown
# settimanale come soglia di pausa.
WEEKLY_DRAWDOWN_CAP_EUR: float = 20.0


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
        max_loss_per_trade_eur=float(
            os.environ.get("MAX_LOSS_PER_TRADE_EUR", "5")
        ),
    )
