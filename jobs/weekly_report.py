"""Entry point del report performance settimanale.

Esecuzione:
    python -m jobs.weekly_report            # invia su Telegram
    python -m jobs.weekly_report --dry-run  # stampa su stdout, no Telegram

Girato come cron (domenica 20:00 Europe/Rome) legge il DB Supabase,
calcola hit-rate per score e per classe asset, genera suggerimenti
automatici e manda un messaggio Telegram formattato.
"""

from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.db import Database
from src.performance import (
    BucketStats,
    compute_hit_rate_by_asset_class,
    compute_hit_rate_by_score,
    compute_weekly_summary,
    suggest_from_asset_buckets,
    suggest_from_score_buckets,
)
from src.telegram_client import TelegramClient

log = logging.getLogger(__name__)

# Soglia sotto la quale la finestra 30gg e' troppo scarsa e usiamo
# 'dall'inizio' come view principale.
MIN_SAMPLE_FOR_30D = 5


def _fmt_eur(x: float) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}€{x:.2f}"


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _fmt_score_table(buckets: list[BucketStats]) -> str:
    rows = [b for b in buckets if b.n > 0]
    if not rows:
        return "  (nessun trade con score nella finestra)"
    out: list[str] = []
    for b in rows:
        out.append(
            f"  <code>{b.label}</code>: {b.wins}/{b.n} "
            f"({_fmt_pct(b.hit_rate)})  avg {_fmt_eur(b.avg_pnl)}"
        )
    return "\n".join(out)


def _fmt_asset_table(buckets: list[BucketStats]) -> str:
    rows = [b for b in buckets if b.n > 0]
    if not rows:
        return "  (nessun trade nella finestra)"
    out: list[str] = []
    for b in rows:
        out.append(
            f"  <code>{b.label}</code>: {b.wins}/{b.n} "
            f"({_fmt_pct(b.hit_rate)})  tot {_fmt_eur(b.total_pnl)}"
        )
    return "\n".join(out)


def build_report(db: Database) -> str:
    summary_7 = compute_weekly_summary(db, days=7)
    score_30, n_30 = compute_hit_rate_by_score(db, days=30)
    asset_30 = compute_hit_rate_by_asset_class(db, days=30)

    header = (
        f"📊 <b>Report settimanale TradeAlert</b>\n\n"
        f"<b>Ultimi 7 giorni:</b>\n"
        f"  Trade chiusi: {summary_7.n_trades} "
        f"({summary_7.wins}W / {summary_7.losses}L)\n"
        f"  P&amp;L netto: <b>{_fmt_eur(summary_7.total_pnl)}</b>\n"
        f"  Win rate: {_fmt_pct(summary_7.win_rate)}\n"
        f"  Max drawdown: {_fmt_eur(summary_7.max_drawdown)}"
    )

    # Se la finestra 30gg e' scarna, affianchiamo i dati 'dall'inizio'.
    fallback_section = ""
    score_buckets_for_suggest = score_30
    asset_buckets_for_suggest = asset_30
    if n_30 < MIN_SAMPLE_FOR_30D:
        score_all, n_all = compute_hit_rate_by_score(db, days=None)
        asset_all = compute_hit_rate_by_asset_class(db, days=None)
        fallback_section = (
            f"\n\n<b>Dall'inizio del tracking ({n_all} trade):</b>\n"
            f"<b>Hit rate per score:</b>\n"
            f"{_fmt_score_table(score_all)}\n"
            f"<b>Per asset class:</b>\n"
            f"{_fmt_asset_table(asset_all)}"
        )
        score_buckets_for_suggest = score_all
        asset_buckets_for_suggest = asset_all

    body_30 = (
        f"\n\n<b>Ultimi 30 giorni ({n_30} trade):</b>\n"
        f"<b>Hit rate per score:</b>\n"
        f"{_fmt_score_table(score_30)}\n"
        f"<b>Per asset class:</b>\n"
        f"{_fmt_asset_table(asset_30)}"
    )

    suggestions = (
        suggest_from_score_buckets(score_buckets_for_suggest)
        + suggest_from_asset_buckets(asset_buckets_for_suggest)
    )
    suggest_block = ""
    if suggestions:
        suggest_block = (
            "\n\n<b>Suggerimenti:</b>\n"
            + "\n".join(f"  • {s}" for s in suggestions)
        )
    else:
        suggest_block = (
            "\n\n<i>Campione ancora ridotto: nessun suggerimento "
            "automatico affidabile per ora.</i>"
        )

    return header + body_30 + fallback_section + suggest_block


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dry_run = "--dry-run" in sys.argv

    config = load_config()
    db = Database(config)
    report = build_report(db)

    if dry_run:
        print(report)
        return 0

    telegram = TelegramClient(config)
    telegram.send_message(report)
    log.info("Weekly report inviato su Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
