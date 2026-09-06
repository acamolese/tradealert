"""Genera la pagina pubblica di ClaudeTrade e la scrive dove nginx la serve.

Scelta 2026-09-06: la pagina sta sulla VM, non su un servizio esterno. Qui i
dati non arrivano da un database interrogato dal browser: vengono scritti
dentro l'HTML a ogni generazione, cosi' la pagina e' un file statico che
chiunque puo' aprire senza credenziali.

Il saldo vero del conto NON finisce nella pagina: esce solo il valore
rapportato al capitale dichiarato.

Uso:
  python -m jobs.claudetrade_page                     # scrive dove serve nginx
  python -m jobs.claudetrade_page --out /tmp/x.html   # altrove
  python -m jobs.claudetrade_page --print             # solo i dati, per capire
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config
from src.grid_esercizio import leggi, valore
from src.grid_report import raccogli

log = logging.getLogger(__name__)
RADICE = Path(__file__).resolve().parent.parent
TEMPLATE = RADICE / "web" / "claudetrade.template.html"
USCITA = Path(os.environ.get("CLAUDETRADE_OUT", "/var/www/claudetrade/index.html"))
PANIERE = ["NL25", "US100", "DE40", "HK50", "J225", "GOLD", "US30", "US500"]


def _arg(nome, default):
    if nome in sys.argv:
        try:
            return sys.argv[sys.argv.index(nome) + 1]
        except IndexError:
            pass
    return default


def paniere(capital) -> list[dict]:
    """Taglia minima, valore e margine di ogni strumento, ai prezzi di adesso."""
    from src.executor import _market_meta
    from src.risk import quote_to_ref_factor

    fuori = []
    for epic in PANIERE:
        try:
            mk = capital.get_market(epic)
            meta = _market_meta(mk)
            px = float((mk.get("snapshot") or {}).get("bid") or 0)
            q2r = quote_to_ref_factor((mk.get("instrument") or {}).get("currency"),
                                      capital) or 1.0
            fuori.append({"epic": epic, "min_size": meta["min_size"],
                          "nozionale": round(meta["min_size"] * px * q2r, 2),
                          "margine": round(meta["min_size"] * px
                                           * meta["margin_factor"] * q2r, 2)})
        except Exception:
            log.exception("paniere: %s non leggibile", epic)
    return fuori


def dati(capital, env: str = "demo") -> dict:
    """Prima della partenza la pagina esiste lo stesso, in attesa: mostra le
    regole e quello che c'e' sul conto, senza valore né registro."""
    from src.grid_esercizio import CAPITALE_DEFAULT

    st = leggi(env)
    avviato = bool(st.get("capitale"))
    cap = float(st.get("capitale") or CAPITALE_DEFAULT)
    c = raccogli(capital, env, n_ultimi=0, con_valore=True)

    stato = {"aggiornato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "capitale": cap, "avviato": avviato, "soglie": {},
             "fase": ("fermo" if c.bloccato else "corso") if avviato else "attesa"}
    if not avviato:
        stato["avvio_previsto"] = ("appena riaprono i mercati, "
                                   "nella notte tra domenica e lunedì")
        stato["valore"] = cap
    if c.ok:
        if avviato:
            stato["valore"] = round(valore(st, c.equity), 2)
            stato["delta_oggi"] = round(c.guadagno_oggi, 2)
        stato["esposto"] = round(sum(p.get("valore") or 0 for p in c.posizioni), 2)
        stato["posizioni"] = [{"epic": p["epic"], "size": p["size"],
                               "valore": round(p.get("valore") or 0, 2),
                               "pnl": round(p["pnl"], 2)} for p in c.posizioni]
    from src.grid_control import soglie_conto
    s = soglie_conto(env)
    stato["soglie"] = {"stop": s["loss_stop"], "obiettivo": s["profit_stop"],
                       "avviso_perdita": s["loss_alert"],
                       "avviso_profitto": s["profit_alert"],
                       "kill_strumento": float(os.environ.get("G2_DNAS_KILL_PNL_EUR", 8))}

    return {"stato": stato,
            "regole": {"capitale": cap, "max_unita": 2, "passo": 0.03,
                       "ema_giorni": 5, "cadenza_minuti": 30,
                       "strumenti": paniere(capital)},
            "giorni": st.get("giorni", [])}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ["CAPITAL_ENV"] = "demo"
    from src.capital_client import CapitalClient
    cfg = load_config()
    cap = CapitalClient(cfg)
    cap.login()

    d = dati(cap)
    if "--print" in sys.argv:
        print(json.dumps(d, indent=1, ensure_ascii=False))
        return 0

    html = TEMPLATE.read_text()
    # </script> dentro una stringa JSON chiuderebbe il tag: unica insidia
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("<!--DATI-->",
                        "<script>window.CLAUDETRADE = " + payload + ";</script>")

    out = Path(_arg("--out", str(USCITA)))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html)
    tmp.replace(out)          # scambio atomico: nessuno legge un file a meta'
    log.info("scritta %s (%d byte)", out, len(html))
    return 0


if __name__ == "__main__":
    sys.exit(main())
