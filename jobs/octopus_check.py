"""Monitor giornaliero del prezzo Luce Fissa di Octopus Energy IT.

Utility TEMPORANEA (scelta tariffa entro fine luglio 2026), indipendente dal
trading: legge https://octopusenergy.it/le-nostre-tariffe, estrae la materia
prima €/kWh della tariffa "Octopus Fissa 12M" (Luce monoraria) e manda OGNI
giorno il prezzo su Telegram (bot tradealert), con celebrazione quando <=
soglia e alert se non riesce a leggerlo. Riusa TelegramClient.

    python -m jobs.octopus_check            # check + Telegram + stato
    python -m jobs.octopus_check --dry-run  # fetch+parse, stampa, niente invio

Cron suggerito: 30 8 * * *  (08:30 Europe/Rome). Rimuovere a fine luglio.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

from src.config import load_config
from src.telegram_client import TelegramClient

URL = "https://octopusenergy.it/le-nostre-tariffe"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
THRESHOLD = 0.1232          # soglia: la tariffa attuale dell'utente
TARIFF = "Octopus Fissa 12M (Luce monoraria)"
STATE = os.path.expanduser("~/tradealert/data/octopus_state.json")
PLAUSIBLE = (0.05, 0.40)    # range di sanita' del prezzo €/kWh


def _to_float(s: str) -> float | None:
    try:
        return float(s.strip().replace(".", "").replace(",", "."))
    except Exception:
        return None


def fetch_price() -> tuple[float | None, str]:
    """Ritorna (prezzo €/kWh della Luce Fissa, nota). Prezzo None se non
    estraibile in modo affidabile."""
    try:
        r = requests.get(URL, headers={"User-Agent": UA}, timeout=25)
    except Exception as e:
        return None, f"fetch error: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    html = r.text
    candidates: list[float] = []
    # 1) campo strutturato: SINGLE_RATE ... consumptionCharge (il piu' stabile)
    for m in re.finditer(
        r'SINGLE_RATE"[^}]{0,120}?"consumptionCharge":"([0-9.,]+)"', html
    ):
        v = _to_float(m.group(1))
        if v and PLAUSIBLE[0] <= v <= PLAUSIBLE[1]:
            candidates.append(v)
    # 2) testo display: blocco "Octopus Fissa 12M ... Materia prima: X €/kWh"
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    m = re.search(
        r"Octopus Fissa 12M\s+Electricity\s+Luce monoraria\s+Materia prima:\s*"
        r"([0-9.,]+)\s*€/kWh",
        txt,
    )
    disp = _to_float(m.group(1)) if m else None
    if disp and PLAUSIBLE[0] <= disp <= PLAUSIBLE[1]:
        candidates.append(disp)
    if not candidates:
        return None, "prezzo non trovato (pagina cambiata o bloccata)"
    # preferisci il valore display se coerente col primo strutturato; in caso
    # di disaccordo prendi il piu' basso plausibile e segnala
    price = disp if disp is not None else candidates[0]
    note = "ok"
    if disp is not None and candidates and abs(disp - candidates[0]) > 0.0005:
        note = f"disaccordo fonti {sorted(set(candidates))}, uso display {disp}"
    return round(price, 6), note


def _load_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)


def main() -> int:
    """Invia OGNI giorno il prezzo della Luce Fissa: un messaggio per run.
    Celebrazione speciale quando <= soglia, alert se non legge il prezzo."""
    dry = "--dry-run" in sys.argv
    today = datetime.now(timezone.utc).date().isoformat()
    price, note = fetch_price()
    st = _load_state()

    if price is None:
        msg = (
            f"⚠️ <b>Luce Fissa Octopus</b>: oggi non riesco a leggere il prezzo "
            f"({note}). Controlla a mano:\n{URL}"
        )
        st["last_check"] = today
        st["last_error_date"] = today
        _emit(dry, [msg], st, None, note)
        return 0

    lowest = st.get("lowest_seen")
    is_new_low = lowest is None or price < lowest - 1e-9
    if is_new_low:
        st["lowest_seen"] = price
    seen_low = st["lowest_seen"]
    dist = price - THRESHOLD

    if price <= THRESHOLD:
        msg = (
            f"🎉 <b>Luce Fissa oggi {price:.4f} €/kWh</b> — ≤ soglia {THRESHOLD:.4f}!\n"
            f"È il momento buono per bloccarla: replichi/superi la tua tariffa attuale.\n{URL}"
        )
    else:
        nl = " 📉 <b>nuovo minimo!</b>" if (is_new_low and lowest is not None) else ""
        msg = (
            f"🐙 <b>Luce Fissa oggi {price:.4f} €/kWh</b> "
            f"(soglia {THRESHOLD:.4f}, manca {dist:+.4f}; minimo visto {seen_low:.4f}).{nl}"
        )

    st["last_price"] = price
    st["last_check"] = today
    _emit(dry, [msg], st, price, note)
    return 0


def _emit(dry, msgs, st, price=None, note=""):
    if dry:
        print(f"[dry-run] prezzo={price} note={note}")
        print(f"[dry-run] stato_nuovo={json.dumps(st)}")
        for m in msgs:
            print("---\n" + m)
        if not msgs:
            print("(nessun messaggio da inviare)")
        return
    if msgs:
        tg = TelegramClient(load_config())
        for m in msgs:
            tg.send_message(m)
    _save_state(st)


if __name__ == "__main__":
    sys.exit(main())
