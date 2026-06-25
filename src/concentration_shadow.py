"""Sprint 5 — regola dinamica "no doppione su tesi che fallisce" (SHADOW).

LOGGING-ONLY: a ogni nuova apertura registra cosa la regola *avrebbe* fatto,
senza bloccare nulla e senza toccare il trade. Pura osservazione, indipendente
da V1 e dal tetto statico. Non viola "un esperimento alla volta": non agisce.

Regola osservata: un nuovo setup sarebbe bloccato se esiste una posizione
aperta CORRELATA con R corrente < SOFT_R_THRESHOLD (-0.3R) al momento del nuovo
setup. Due definizioni di "correlato" loggate in PARALLELO (non si sceglie ora):
  - corr_A: stesso asset + stessa direzione
  - corr_B: stessa direzione + stessa classe (commodity/equity/crypto/fx)

Per ogni nuovo trade si scrive un record con would_block_A, would_block_B, l'R
della posizione correlata e gli id di entrambi. Verdetto pre-registrato (da
rivedere dopo): per ogni would_block, il trade lasciato passare com'e' finito?
La regola "aveva ragione" se quel trade e' poi andato in perdita. Confronto A vs
B: quale separa meglio i loss senza tagliare i winner.

Record scritti in ``monitoring_events`` con event_type='concentration_shadow'.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import Database
from .universe import ALL_KNOWN

log = logging.getLogger(__name__)

SOFT_R_THRESHOLD = -0.3  # una correlata sotto questo R "avrebbe bloccato"

# Classe larga per corr_B: raggruppa le asset_class fini dell'universo.
_BROAD_CLASS = {
    "metal": "commodity",
    "energy": "commodity",
    "index": "equity",
    "crypto": "crypto",
    "fx": "fx",
}
_ASSET_BROAD = {a.name: _BROAD_CLASS.get(a.asset_class, a.asset_class) for a in ALL_KNOWN}


def _current_r(db: Database, trade: dict[str, Any]) -> float | None:
    """R corrente di un trade aperto, dall'ultimo intra_trade_extreme (cheap,
    nessuna chiamata Capital). None se non determinabile."""
    sig = db.get_signal(trade["signal_id"]) if trade.get("signal_id") else None
    if not sig or not sig.get("stop_loss") or not trade.get("entry_price"):
        return None
    entry = float(trade["entry_price"])
    r_dist = entry * float(sig["stop_loss"]) / 100.0
    if r_dist <= 0:
        return None
    ev = db.get_last_monitoring_event(trade["id"], "intra_trade_extreme")
    last = (ev.get("details") or {}).get("last_price") if ev else None
    if last is None:
        return 0.0  # appena aperto, nessun movimento loggato ancora
    last = float(last)
    if trade["direction"] == "short":
        return round((entry - last) / r_dist, 3)
    return round((last - entry) / r_dist, 3)


def log_shadow(config: Any, db: Database, new_trade: dict[str, Any]) -> None:
    """Best-effort: registra il record shadow per ``new_trade``. Non solleva
    mai (avvolto in try/except dal chiamante), non blocca, non tocca trade."""
    if not getattr(config, "concentration_shadow", True):
        return
    new_asset = new_trade.get("asset")
    new_dir = new_trade.get("direction")
    new_class = _ASSET_BROAD.get(new_asset)

    others = [
        t for t in db.get_open_trades()
        if t.get("id") != new_trade.get("id")
    ]

    corr_a: list[tuple[int, float | None]] = []  # (trade_id, R) stesso asset+dir
    corr_b: list[tuple[int, float | None]] = []  # (trade_id, R) stessa dir+classe
    for t in others:
        if t.get("direction") != new_dir:
            continue
        r = _current_r(db, t)
        if t.get("asset") == new_asset:
            corr_a.append((t["id"], r))
        if _ASSET_BROAD.get(t.get("asset")) == new_class:
            corr_b.append((t["id"], r))

    def _worst(pairs: list[tuple[int, float | None]]):
        valid = [(tid, r) for tid, r in pairs if r is not None]
        if not valid:
            return None, None
        tid, r = min(valid, key=lambda x: x[1])  # R più basso = peggiore
        return tid, r

    a_id, a_r = _worst(corr_a)
    b_id, b_r = _worst(corr_b)
    would_block_a = a_r is not None and a_r < SOFT_R_THRESHOLD
    would_block_b = b_r is not None and b_r < SOFT_R_THRESHOLD

    db.insert_monitoring_event({
        "trade_id": new_trade.get("id"),
        "event_type": "concentration_shadow",
        "reason": (
            f"new {new_asset} {new_dir} | A blk={would_block_a} (R={a_r}) "
            f"| B blk={would_block_b} (R={b_r})"
        ),
        "details": {
            "new_asset": new_asset,
            "new_direction": new_dir,
            "new_class": new_class,
            "soft_r_threshold": SOFT_R_THRESHOLD,
            "would_block_A": would_block_a,
            "corr_A_trade_id": a_id,
            "corr_A_R": a_r,
            "would_block_B": would_block_b,
            "corr_B_trade_id": b_id,
            "corr_B_R": b_r,
        },
    })
    log.info(
        "shadow concentration: %s %s | A=%s(R=%s) B=%s(R=%s)",
        new_asset, new_dir, would_block_a, a_r, would_block_b, b_r,
    )
