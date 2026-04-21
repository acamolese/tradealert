"""Analisi performance trade chiusi per il report settimanale.

Tre funzioni principali:
- ``compute_hit_rate_by_score``: raggruppa per bucket di score del signal
  originale e calcola hit-rate + P&L medio per bucket.
- ``compute_hit_rate_by_asset_class``: stesso, ma per classe asset
  (metal/energy/index/fx/crypto/other).
- ``compute_weekly_summary``: P&L totale, n. trade, win rate, max drawdown
  per una finestra di giorni.

Tutte le funzioni accettano un parametro ``days`` (finestra di look-back).
Quando la finestra e' troppo scarna, il caller puo' ripetere con
``days=None`` per avere i dati dall'inizio del tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .db import Database
from .universe import UNIVERSE


# --- Bucket di score per l'analisi di calibrazione ---
SCORE_BUCKETS: list[tuple[float, float, str]] = [
    (6.0, 7.0, "6.0-7.0"),
    (7.0, 7.5, "7.0-7.5"),
    (7.5, 8.0, "7.5-8.0"),
    (8.0, 9.0, "8.0-9.0"),
    (9.0, 10.01, "9.0-10"),
]


# --- Mappa asset_name -> asset_class (fallback quando non in UNIVERSE) ---
_UNIVERSE_CLASS: dict[str, str] = {a.name: a.asset_class for a in UNIVERSE}


def _infer_asset_class(asset_name: str | None, epic: str | None = None) -> str:
    """Ritorna la classe dell'asset. Gli asset dinamici trovati dal
    discovery Capital (token crypto pescati runtime) non stanno in
    UNIVERSE: se il nome ha il pattern 'XXX/USD' o l'epic finisce per
    'USD', assumiamo crypto; altrimenti 'other'."""
    if not asset_name:
        return "other"
    if asset_name in _UNIVERSE_CLASS:
        return _UNIVERSE_CLASS[asset_name]
    if "/USD" in asset_name or (epic or "").upper().endswith("USD"):
        return "crypto"
    return "other"


@dataclass
class BucketStats:
    label: str
    n: int
    wins: int
    total_pnl: float

    @property
    def hit_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.n if self.n else 0.0


def _fetch_closed_trades(
    db: Database, days: int | None
) -> list[dict[str, Any]]:
    """Trade chiusi nella finestra richiesta. days=None -> tutto lo
    storico disponibile."""
    query = db._client.table("trades").select("*").eq("status", "closed")
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.gte("closed_at", cutoff.isoformat())
    return query.order("closed_at", desc=False).execute().data or []


def _enrich_with_score(
    db: Database, trades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Aggiunge ``_score`` (score del signal originale, None se manca).
    Fa una query per signal_id: per poche decine di trade e' fine."""
    signal_ids = [
        t["signal_id"] for t in trades if t.get("signal_id") is not None
    ]
    score_map: dict[int, float] = {}
    if signal_ids:
        resp = (
            db._client.table("signals")
            .select("id,score")
            .in_("id", signal_ids)
            .execute()
        )
        for row in resp.data or []:
            if row.get("score") is not None:
                score_map[row["id"]] = float(row["score"])
    for t in trades:
        sid = t.get("signal_id")
        t["_score"] = score_map.get(sid) if sid is not None else None
    return trades


def _pnl_of(trade: dict[str, Any]) -> float:
    """P&L in EUR. Alcuni trade vecchi hanno pnl=None ma pnl_pct valorizzato:
    in quel caso ricostruisce pnl da size*entry*pnl_pct/100."""
    pnl = trade.get("pnl")
    if pnl is not None:
        return float(pnl)
    pnl_pct = trade.get("pnl_pct")
    size = trade.get("size") or 0
    entry = trade.get("entry_price") or 0
    if pnl_pct is not None and size and entry:
        return float(size) * float(entry) * float(pnl_pct) / 100
    return 0.0


def compute_hit_rate_by_score(
    db: Database, days: int | None = 30
) -> tuple[list[BucketStats], int]:
    """Ritorna (stats_per_bucket, n_trade_totali)."""
    trades = _enrich_with_score(db, _fetch_closed_trades(db, days))
    buckets = {
        label: BucketStats(label=label, n=0, wins=0, total_pnl=0.0)
        for _, _, label in SCORE_BUCKETS
    }
    for t in trades:
        score = t.get("_score")
        if score is None:
            continue
        for lo, hi, label in SCORE_BUCKETS:
            if lo <= score < hi:
                pnl = _pnl_of(t)
                b = buckets[label]
                b.n += 1
                if pnl > 0:
                    b.wins += 1
                b.total_pnl += pnl
                break
    return list(buckets.values()), len(trades)


def compute_hit_rate_by_asset_class(
    db: Database, days: int | None = 30
) -> list[BucketStats]:
    trades = _fetch_closed_trades(db, days)
    groups: dict[str, BucketStats] = {}
    for t in trades:
        cls = _infer_asset_class(t.get("asset"), t.get("capital_deal_id"))
        pnl = _pnl_of(t)
        b = groups.setdefault(
            cls, BucketStats(label=cls, n=0, wins=0, total_pnl=0.0)
        )
        b.n += 1
        if pnl > 0:
            b.wins += 1
        b.total_pnl += pnl
    return sorted(groups.values(), key=lambda x: x.n, reverse=True)


@dataclass
class WeeklySummary:
    window_days: int | None
    n_trades: int
    wins: int
    losses: int
    total_pnl: float
    max_drawdown: float  # peggior drawdown in EUR sulla equity curve

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_trades if self.n_trades else 0.0


def compute_weekly_summary(
    db: Database, days: int | None = 7
) -> WeeklySummary:
    trades = _fetch_closed_trades(db, days)
    pnls = [_pnl_of(t) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total = sum(pnls)
    # Max drawdown: min sulla curva cumulata meno il suo running peak.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = equity - peak
        if dd < max_dd:
            max_dd = dd
    return WeeklySummary(
        window_days=days,
        n_trades=len(trades),
        wins=wins,
        losses=losses,
        total_pnl=total,
        max_drawdown=max_dd,
    )


# --- Suggerimenti automatici a partire dai bucket ---

def suggest_from_score_buckets(
    buckets: list[BucketStats], min_sample: int = 3
) -> list[str]:
    """Genera suggerimenti testuali leggendo i bucket. Usa min_sample
    per evitare conclusioni da 1-2 trade. Ritorna lista di stringhe."""
    out: list[str] = []
    # Se il bucket 7.0-7.5 ha >= min_sample trade e hit rate < 40%, suggerisci
    # di alzare MIN_SCORE_THRESHOLD a 7.5.
    low = next((b for b in buckets if b.label == "7.0-7.5"), None)
    if low and low.n >= min_sample and low.hit_rate < 0.40:
        out.append(
            f"Alza MIN_SCORE_THRESHOLD a 7.5: bucket 7.0-7.5 ha "
            f"hit rate {low.hit_rate:.0%} su {low.n} trade."
        )
    high = next((b for b in buckets if b.label == "8.0-9.0"), None)
    if high and high.n >= min_sample and high.hit_rate >= 0.60:
        out.append(
            f"Bucket 8.0-9.0 sta performando bene "
            f"({high.hit_rate:.0%} su {high.n}): considera di aumentare "
            f"esposizione sui setup con score >= 8."
        )
    return out


def suggest_from_asset_buckets(
    buckets: list[BucketStats], min_sample: int = 3
) -> list[str]:
    out: list[str] = []
    for b in buckets:
        if b.n >= min_sample and b.hit_rate == 0.0:
            out.append(
                f"Classe '{b.label}': 0 win su {b.n} trade. "
                f"Valuta di escluderla temporaneamente."
            )
    return out
