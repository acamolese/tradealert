"""Scarica lo storico candele Capital per il backtest harness (Sprint 7 M1).

Copertura rilevata il 2026-07-02: DAY dal 2015, HOUR/HOUR_4 dal 2020,
MINUTE_30 dal 2024. Salva CSV gzip in data/candles/{EPIC}_{RES}.csv.gz con
colonne: ts, open_bid, open_ask, high_bid, high_ask, low_bid, low_ask,
close_bid, close_ask, volume. Incrementale: se il file esiste riparte
dall'ultima candela salvata.

Uso: PYTHONPATH=$PWD .venv/bin/python jobs/fetch_history.py [EPIC ...]
     (default: i 5 core; res di default DAY e HOUR)
"""

from __future__ import annotations

import csv
import gzip
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.config import load_config
from src.capital_client import CapitalClient

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "candles"
DEFAULT_EPICS = ["GOLD", "OIL_BRENT", "US500", "US100", "BTCUSD"]
RES_START = {"DAY": "2015-01-01", "HOUR": "2020-01-01"}
# span per request calibrato su max=1000 candele
RES_SPAN_DAYS = {"DAY": 900, "HOUR": 35}

FIELDS = ["ts", "open_bid", "open_ask", "high_bid", "high_ask",
          "low_bid", "low_ask", "close_bid", "close_ask", "volume"]


def _row(c: dict) -> list:
    def g(k, side):
        v = c.get(k) or {}
        return v.get(side)
    return [c["snapshotTimeUTC"],
            g("openPrice", "bid"), g("openPrice", "ask"),
            g("highPrice", "bid"), g("highPrice", "ask"),
            g("lowPrice", "bid"), g("lowPrice", "ask"),
            g("closePrice", "bid"), g("closePrice", "ask"),
            c.get("lastTradedVolume")]


def fetch_epic(cl: CapitalClient, cfg, epic: str, res: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{epic}_{res}.csv.gz"
    last_ts = None
    if out.exists():
        with gzip.open(out, "rt") as f:
            for line in f:
                pass
            if not line.startswith("ts"):
                last_ts = line.split(",")[0]
    start = (datetime.fromisoformat(last_ts) + timedelta(seconds=1)
             if last_ts else datetime.fromisoformat(RES_START[res]))
    end = datetime.utcnow()
    span = timedelta(days=RES_SPAN_DAYS[res])
    mode = "at" if out.exists() else "wt"
    n = 0
    with gzip.open(out, mode) as f:
        w = csv.writer(f)
        if mode == "wt":
            w.writerow(FIELDS)
        cur = start
        while cur < end:
            to = min(cur + span, end)
            for attempt in (1, 2):
                r = cl._session.get(
                    cl._url(f"/prices/{epic}"), headers=cl._auth_headers(),
                    params={"resolution": res,
                            "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                            "to": to.strftime("%Y-%m-%dT%H:%M:%S"),
                            "max": 1000},
                    timeout=30)
                if r.status_code == 200:
                    break
                if r.status_code in (401, 403) and attempt == 1:
                    cl.login()  # sessione scaduta durante il download
                    continue
                if r.status_code == 429:
                    time.sleep(2)
                    continue
            prices = r.json().get("prices", []) if r.status_code == 200 else []
            for c in prices:
                if last_ts is None or c["snapshotTimeUTC"] > last_ts:
                    w.writerow(_row(c))
                    n += 1
            if prices:
                last_ts = prices[-1]["snapshotTimeUTC"]
            cur = to
            time.sleep(0.25)
    print(f"{epic} {res}: +{n} candele -> {out.name}")


def main() -> int:
    cfg = load_config()
    cl = CapitalClient(cfg)
    cl.login()
    epics = sys.argv[1:] or DEFAULT_EPICS
    for epic in epics:
        for res in RES_START:
            fetch_epic(cl, cfg, epic, res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
