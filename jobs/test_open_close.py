"""Test ad-hoc weekend: apre una posizione minima su BTCUSD e la richiude
subito. Serve solo a validare end-to-end l'integrazione di trading con
Capital.com quando i mercati tradizionali sono chiusi.

Uso:
    python -m jobs.test_open_close [epic]

Default: BTCUSD (sempre aperto). Puoi passare ETHUSD o un altro epic.

Costo atteso: lo spread bid/offer al momento dell'apertura, quindi pochi
centesimi. Se il demo ha saldo zero, fallisce con messaggio chiaro.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback

from src.capital_client import CapitalAPIError, CapitalClient
from src.config import load_config
from src.executor import _market_meta
from src.telegram_client import TelegramClient


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    epic = sys.argv[1] if len(sys.argv) > 1 else "BTCUSD"
    config = load_config()
    capital = CapitalClient(config)
    telegram = TelegramClient(config)

    try:
        capital.login()
        market = capital.get_market(epic)
        meta = _market_meta(market)
        snap = market.get("snapshot", {})

        if snap.get("marketStatus") != "TRADEABLE":
            print(f"{epic} non e' TRADEABLE: {snap.get('marketStatus')}")
            return 1

        size = float(meta["min_size"])
        bid = float(snap["bid"])
        offer = float(snap["offer"])
        mid = (bid + offer) / 2

        # SL e TP volutamente larghi (5%) per evitare touch immediato.
        stop_level = round(mid * 0.95, 2)
        profit_level = round(mid * 1.05, 2)

        print(f"Apro posizione test: epic={epic} BUY size={size} entry~{mid}")
        deal = capital.create_position(
            epic=epic,
            direction="BUY",
            size=size,
            stop_level=stop_level,
            profit_level=profit_level,
        )
        deal_ref = deal.get("dealReference")
        print(f"dealReference: {deal_ref}")

        confirm = capital.confirm_deal(deal_ref) if deal_ref else {}
        print(f"confirm: {confirm}")

        # Capital ritorna in dealId l'ID dell'ORDINE; il position id vero
        # e' in affectedDeals[0].dealId. In fallback, leggiamo le posizioni
        # aperte e cerchiamo per dealReference.
        deal_id = None
        affected = confirm.get("affectedDeals") or []
        if affected:
            deal_id = affected[0].get("dealId")
        if not deal_id:
            deal_id = confirm.get("dealId")
        status = confirm.get("dealStatus")
        if status != "ACCEPTED" or not deal_id:
            telegram.send_message(
                f"⚠️ <b>Test {epic}: apertura non accettata</b>\n"
                f"<pre>{confirm}</pre>"
            )
            return 1

        # Verifica recuperando la posizione effettivamente aperta
        try:
            positions = capital.get_open_positions()
            for p in positions:
                pos = p.get("position", {}) or {}
                if pos.get("dealReference") == deal_ref:
                    deal_id = pos.get("dealId") or deal_id
                    break
        except Exception as exc:
            print(f"WARN: get_open_positions fallito: {exc}")

        print(f"deal_id (per close): {deal_id}")

        telegram.send_message(
            f"✅ <b>Test {epic}: posizione aperta</b>\n"
            f"Deal: <code>{deal_id}</code>\n"
            f"Size: {size}\n"
            f"Entry approx: {confirm.get('level')}\n"
            f"Chiusura tra 10 secondi..."
        )

        time.sleep(10)

        print(f"Chiudo posizione {deal_id}")
        close_resp = capital.close_position(deal_id)
        close_ref = close_resp.get("dealReference")
        close_confirm = (
            capital.confirm_deal(close_ref) if close_ref else {}
        )
        print(f"close confirm: {close_confirm}")

        pnl = close_confirm.get("profit") or close_confirm.get(
            "profitAndLoss"
        )
        telegram.send_message(
            f"🔚 <b>Test {epic}: posizione chiusa</b>\n"
            f"Prezzo chiusura: {close_confirm.get('level')}\n"
            f"P&amp;L: {pnl}"
        )
        return 0
    except CapitalAPIError as exc:
        msg = f"Capital API errore {exc.status}: {exc.body}"
        print(msg)
        try:
            telegram.send_message(f"🚨 <b>Test fallito</b>\n<pre>{msg}</pre>")
        except Exception:
            traceback.print_exc()
        return 1
    except Exception as exc:
        print(f"Errore: {exc}")
        traceback.print_exc()
        try:
            telegram.send_message(f"🚨 <b>Test fallito</b>\n<pre>{exc}</pre>")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
