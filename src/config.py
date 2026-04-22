"""Carica configurazione da variabili d'ambiente."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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
    )
