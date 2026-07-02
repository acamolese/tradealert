"""Backfill di trades.risk_at_open_eur e trades.exit_r (Sprint 6 A4.1).

Best-effort sui trade storici: rischio ricostruito da signal.stop_loss (pct)
per size (approssimazione: non conosce eventuali allargamenti min-distance del
broker), convertito nella valuta di riferimento via quote_to_ref_factor per i
quote non-USD (HKD, JPY). exit_r = pnl / risk per i trade chiusi con pnl.
I trade senza signal collegato restano NULL e vengono conteggiati.

Richiede SUPABASE_SERVICE_ROLE_KEY (RLS): eseguire sulla VM.
Uso: PYTHONPATH=$PWD .venv/bin/python jobs/backfill_risk_r.py [--dry-run]
"""

from __future__ import annotations

import sys

from src.config import load_config
from src.db import Database
from src.capital_client import CapitalClient
from src.risk import quote_to_ref_factor


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = load_config()
    db = Database(cfg)
    if not db.trades_risk_columns_available():
        print("Colonne risk_at_open_eur/exit_r assenti: applicare prima la "
              "migration 20260702150000_trades_risk_r.sql")
        return 1
    cl = CapitalClient(cfg)
    cl.login()

    trades = (db._client.table("trades").select("*")
              .is_("risk_at_open_eur", "null").order("id").execute().data)
    print(f"trade senza risk_at_open_eur: {len(trades)}")

    ccy_cache: dict[str, float | None] = {}

    def ref_factor(epic: str | None) -> float | None:
        if not epic:
            return None
        if epic not in ccy_cache:
            try:
                market = cl.get_market(epic)
                ccy = (market.get("instrument", {}) or {}).get("currency")
                ccy_cache[epic] = quote_to_ref_factor(ccy, cl)
            except Exception as exc:
                print(f"  warn: {epic}: market/tasso non disponibile ({exc})")
                ccy_cache[epic] = None
        return ccy_cache[epic]

    done, skipped = 0, []
    for t in trades:
        sig = db.get_signal(t["signal_id"]) if t.get("signal_id") else None
        sl_pct = sig.get("stop_loss") if sig else None
        if not sl_pct:
            skipped.append((t["id"], "manca signal/stop_loss"))
            continue
        factor = ref_factor(sig.get("epic"))
        if factor is None:
            skipped.append((t["id"], "tasso quote->ref non determinabile"))
            continue
        entry = float(t["entry_price"])
        risk = entry * float(sl_pct) / 100.0 * float(t["size"]) * factor
        if risk <= 0:
            skipped.append((t["id"], "risk non positivo"))
            continue
        update = {"risk_at_open_eur": round(risk, 4)}
        if t.get("pnl") is not None:
            update["exit_r"] = round(float(t["pnl"]) / risk, 4)
        if dry:
            print(f"  #{t['id']:>3} {t['asset']:<14} risk={risk:.4f} "
                  f"exit_r={update.get('exit_r')}")
        else:
            db._client.table("trades").update(update).eq("id", t["id"]).execute()
        done += 1

    print(f"aggiornati: {done}{' (dry-run)' if dry else ''} | saltati: {len(skipped)}")
    for sid, why in skipped:
        print(f"  saltato #{sid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
