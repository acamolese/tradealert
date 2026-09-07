"""Vende assicurazione sulla volatilita': apre e mantiene la posizione.

Cosa fa, in una riga: sta corto su uno strumento costruito per perdere valore
(UVXY rinnova ogni giorno contratti a termine piu' cari di quelli che scadono,
-79,8% l'anno su quindici anni) e incassa quel decadimento.

Non prevede niente. Il rendimento e' il premio di chi vende protezione: si
guadagna poco quasi sempre e si perde molto raramente. Il 5 febbraio 2018 UVXY
e' salito del 66% in una seduta, e con l'esposizione qui prevista sarebbe stata
una perdita del 10% del capitale in un giorno.

Sicurezze: solo sul conto di prova, mai a mercato chiuso, tetto sull'esposizione,
stop di emergenza con pausa obbligatoria.

Uso:
  python -m jobs.paura_esegui             # apre, mantiene, ribilancia
  python -m jobs.paura_esegui --stato     # guarda e basta
  python -m jobs.paura_esegui --chiudi    # chiude tutto e si ferma
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.grid_control import eur, leggi_equity

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent / "data"


def _f(nome: str, default: float) -> float:
    try:
        return float(os.environ.get(nome, default))
    except ValueError:
        return default


def stato() -> dict:
    try:
        return json.loads((DATA / "paura.json").read_text())
    except Exception:
        return {}


def scrivi(d: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "paura.json").write_text(json.dumps(d, indent=1))


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ["CAPITAL_ENV"] = "demo"          # guard: mai sul conto reale
    cfg = load_config()
    if cfg.capital_env != "demo":
        log.error("questa strategia gira solo sul conto di prova")
        return 1

    epic = os.environ.get("PAURA_EPIC", "UVXY")
    esposizione = _f("PAURA_ESPOSIZIONE", 0.15)     # frazione del capitale
    tetto = _f("PAURA_TETTO", 0.25)                 # mai oltre questo
    stop_eur = _f("PAURA_STOP_EUR", 15.0)
    pausa_gg = int(_f("PAURA_PAUSA_GG", 5))
    banda = _f("PAURA_BANDA", 0.40)                 # ribilancia oltre questo scarto

    from src.capital_client import CapitalClient
    from src.telegram_client import TelegramClient
    from src.executor import _market_meta
    from src.risk import quote_to_ref_factor
    from src.grid_esercizio import leggi as leggi_esercizio

    cap = CapitalClient(cfg)
    cap.login()
    tg = TelegramClient(cfg)
    st = stato()

    es = leggi_esercizio("demo")
    capitale = float(es.get("capitale") or 200.0)

    mk = cap.get_market(epic)
    sn = mk.get("snapshot") or {}
    inst = mk.get("instrument") or {}
    aperto = (sn.get("marketStatus") or "").upper() == "TRADEABLE"
    bid, ask = float(sn.get("bid") or 0), float(sn.get("offer") or 0)
    if not bid or not ask:
        log.info("nessun prezzo per %s", epic)
        return 0
    prezzo = (bid + ask) / 2
    meta = _market_meta(mk)
    q2r = quote_to_ref_factor(inst.get("currency"), cap) or 1.0
    noz_unita = meta["min_size"] * prezzo * q2r

    # posizione attuale
    size, pnl, deals = 0.0, 0.0, []
    for p in cap.get_open_positions():
        po, m = p.get("position", {}), p.get("market", {})
        if m.get("epic") != epic:
            continue
        s = float(po.get("size") or 0)
        size += -s if (po.get("direction") or "").upper() == "SELL" else s
        pnl += float(po.get("upl") or 0)
        deals.append(po.get("dealId"))
    noz_attuale = abs(size) * prezzo * q2r

    if "--stato" in sys.argv:
        print(f"{epic}: prezzo {prezzo:.2f}, mercato "
              f"{'aperto' if aperto else 'chiuso'}")
        print(f"posizione: {size:+.0f} unita = {eur(noz_attuale)} di esposizione, "
              f"risultato {eur(pnl, True)}")
        print(f"obiettivo: {eur(capitale * esposizione)} "
              f"({esposizione:.0%} di {eur(capitale)})")
        if st.get("in_pausa_fino"):
            print(f"in pausa fino al {st['in_pausa_fino'][:10]}")
        return 0

    if "--chiudi" in sys.argv:
        for d in deals:
            cap.close_position(d)
        scrivi({**st, "chiuso_il": datetime.now(timezone.utc).isoformat()})
        tg.send_message(f"⏹️ <b>Assicurazione: chiusa</b>\nChiuse {len(deals)} posizioni.")
        return 0

    if not aperto:
        log.info("%s: mercato chiuso, non tocco nulla", epic)
        return 0

    # stop di emergenza: chiude e resta fermo per qualche giorno
    if size and pnl <= -stop_eur:
        for d in deals:
            cap.close_position(d)
        fino = (datetime.now(timezone.utc) + timedelta(days=pausa_gg)).isoformat()
        scrivi({**st, "in_pausa_fino": fino, "ultimo_stop": pnl})
        tg.send_message(
            f"🛑 <b>Assicurazione: stop</b>\n"
            f"La posizione su {epic} ha perso {eur(-pnl)}, oltre il limite di "
            f"{eur(stop_eur)}. Ho chiuso tutto.\nNon riapro per {pausa_gg} giorni: "
            f"dopo uno strappo la volatilità resta alta e riaprire subito è la "
            f"cosa peggiore.")
        log.warning("STOP: perdita %.2f€, pausa fino al %s", pnl, fino[:10])
        return 0

    if st.get("in_pausa_fino") and datetime.now(timezone.utc).isoformat() < st["in_pausa_fino"]:
        log.info("in pausa fino al %s", st["in_pausa_fino"][:10])
        return 0

    obiettivo = capitale * esposizione
    massimo = capitale * tetto
    if noz_attuale > massimo:
        log.warning("esposizione %.2f€ oltre il tetto %.2f€: riduco", noz_attuale, massimo)
    elif size and abs(noz_attuale / obiettivo - 1) <= banda:
        log.info("%s: esposizione %.2f€ vicina all'obiettivo %.2f€, non tocco",
                 epic, noz_attuale, obiettivo)
        return 0

    unita_target = max(1, round(obiettivo / noz_unita))
    size_target = -unita_target * meta["min_size"]
    delta = size_target - size
    if abs(delta) < meta["min_size"] / 2:
        return 0

    verso = "SELL" if delta < 0 else "BUY"
    # Stop di mercato sul broker: il controllo giornaliero non protegge dai
    # salti notturni, e questo strumento e' salito del 66% in una seduta.
    # Livello scelto perche' la perdita corrisponda al limite in euro.
    noz_target = unita_target * noz_unita
    stop_level = None
    if verso == "SELL" and noz_target > 0:
        stop_level = round(prezzo * (1 + min(0.60, stop_eur / noz_target)), 2)
    log.info("%s: da %+.0f a %+.0f unita (%s %.4f) — esposizione %.2f -> %.2f€, "
             "stop a %s", epic, size, size_target, verso, abs(delta), noz_attuale,
             noz_target, stop_level)
    cap.create_position(epic=epic, direction=verso, size=abs(delta),
                        stop_level=stop_level)
    scrivi({**st, "ultimo_ordine": datetime.now(timezone.utc).isoformat(),
            "unita": unita_target, "in_pausa_fino": None})
    if not st.get("avviata"):
        tg.send_message(
            f"🛡️ <b>Assicurazione: partita</b>\n"
            f"Vendo {unita_target} quote di {epic} allo scoperto: "
            f"{eur(unita_target * noz_unita)} di esposizione su {eur(capitale)}.\n\n"
            f"Non prevede niente: quello strumento perde valore per come è "
            f"costruito, e io incasso quel calo. Si guadagna poco quasi sempre e "
            f"si perde molto di rado: se perde {eur(stop_eur)} chiudo e resto "
            f"fermo {pausa_gg} giorni.")
        scrivi({**stato(), "avviata": datetime.now(timezone.utc).isoformat()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
