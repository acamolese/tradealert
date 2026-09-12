"""Misura in ombra le varianti di esposizione, senza eseguirne nessuna.

Ogni giorno registra il segnale e le tre taglie che tre politiche diverse
avrebbero tenuto:

  fissa    il 15% di sempre, la baseline (quello che il sistema faceva fino al
           2026-09-12, prima della scala condizionata)
  scala    quello che il sistema esegue davvero
  spinta   la stessa scala ma con tetto al 40%, per sapere se valeva la pena
           osare di piu' senza averlo rischiato

Nessun ordine, nessuna posizione: scrive una riga al giorno e basta. Serve al
gate pre-registrato in docs/gate-esposizione-volatilita.md, che dira' fra tre
mesi se la scala e' stata utile o solo complicata.

Uso:
  python -m jobs.vol_shadow            # registra la riga di oggi
  python -m jobs.vol_shadow --print     # mostra e non scrive
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

from src import vol_segnale
from src.vol_config import carica
from src.vol_runner import _database, _mercato, _posizione, conto_da_stato, stato
from src.vol_store import VolStore, features_decisione
from src.volatilita import scala

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ["CAPITAL_ENV"] = "demo"
    from src.capital_client import CapitalClient
    from src.config import load_config

    cfg = load_config()
    if cfg.capital_env != "demo":
        log.error("misura in ombra solo sul conto di prova")
        return 1

    par = carica()
    cap = CapitalClient(cfg)
    cap.login()

    segnale = vol_segnale.leggi(cap)
    mk = _mercato(cap, par.epic)
    size, pnl, _ = _posizione(cap, par.epic)
    conto = conto_da_stato(stato(), pnl)

    g_scala = scala(segnale, conto, par)
    # La variante spinta alza tetto e gradini, lasciando identiche le condizioni:
    # cosi' la differenza misurata e' solo la taglia, non il criterio.
    par_spinta = replace(par, tetto=0.40, frazione_base=0.20,
                         frazione_favorevole=0.30, frazione_piena=0.40)
    g_spinta = scala(segnale, conto, par_spinta)

    riga = {
        "giorno": datetime.now(timezone.utc).date().isoformat(),
        "vix": segnale.vix,
        "vixm": segnale.vixm,
        "pendenza": segnale.pendenza,
        "percentile_vix": segnale.percentile,
        "prezzo_epic": (mk or {}).get("prezzo"),
        "frazione_fissa": par.frazione_base,
        "frazione_scala": g_scala.frazione,
        "frazione_spinta": g_spinta.frazione,
        "gradino_scala": g_scala.nome,
        "gradino_spinta": g_spinta.nome,
        "features": {**features_decisione(segnale, conto),
                     "motivi_scala": g_scala.motivi,
                     "size_reale": size},
    }

    print(f"{riga['giorno']}  {vol_segnale.racconta(segnale)}")
    print(f"  fissa {par.frazione_base:.0%} | scala {g_scala.frazione:.0%} "
          f"({g_scala.nome}) | spinta {g_spinta.frazione:.0%} ({g_spinta.nome})")
    for m in g_scala.motivi:
        print(f"  · {m}")

    if "--print" in sys.argv:
        return 0

    store = VolStore(_database(cfg))
    store.shadow(riga)
    log.info("riga in ombra registrata (%d giorni raccolti)", store.giorni_shadow())
    return 0


if __name__ == "__main__":
    sys.exit(main())
