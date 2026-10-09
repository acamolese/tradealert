"""La scala di ClaudeTrade: misura i soldi vinti e sposta la taglia.

Gira sul conto di prova, due volte:
  * ogni sera feriale (dopo il resoconto delle 22:30): puo' solo SCENDERE, e
    avvisa su Telegram se lo fa;
  * il lunedi' mattina (prima del buongiorno delle 8): puo' anche SALIRE, e
    manda sempre il punto della settimana, anche se non cambia nulla.

Il profitto viene letto dal broker (/history/transactions dall'avvio
dell'esercizio), mai dai file di stato. Lo stato prodotto (data/scalata_demo.json)
e' quello che jobs/grid2.py e src/vol_runner.py leggono per sapere quante taglie
in piu' tenere.

Uso:
  python -m jobs.scalata              # sera: misura, scende se serve
  python -m jobs.scalata --sali       # lunedi': misura, sale o scende
  python -m jobs.scalata --stato      # stampa e non scrive
  aggiungere --print per vedere il messaggio senza inviarlo
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src import scalata
from src.config import load_config
from src.grid_control import eur, nome_strumento
from src.grid_esercizio import leggi as leggi_esercizio, pagina_url

log = logging.getLogger(__name__)
ROMA = ZoneInfo("Europe/Rome")
ENV = "demo"


def _messaggio(esito: scalata.Esito, prec: int, regole: scalata.Regole,
               settimanale: bool) -> str:
    """Il punto della scala, in italiano piano."""
    if esito.gradino > prec:
        titolo = f"📈 <b>Scala: salgo al gradino {esito.gradino}</b>"
    elif esito.gradino < prec:
        titolo = f"📉 <b>Scala: scendo al gradino {esito.gradino}</b>"
    else:
        titolo = f"🪜 <b>Scala: gradino {esito.gradino} di {regole.max_gradini}</b>"
    righe = [titolo, "",
             f"Incassato dall'avvio: <b>{eur(esito.profitto_netto, True)}</b> "
             f"(operazioni chiuse, costi notturni e dividendi, letti dal broker)."]
    if esito.motivi:
        righe.append("; ".join(esito.motivi).capitalize() + ".")
    if esito.gradino >= regole.max_gradini:
        righe.append("La scala è piena: ogni strumento ha già la taglia in più.")
    elif esito.mancano > 0:
        righe.append(f"Prossimo gradino a {eur(esito.mancano)} di profitto in più"
                     + (", si sale solo il lunedì." if not settimanale else "."))
    in_piu = [(e, n) for e, n in esito.extra.items() if n > 0]
    if in_piu:
        righe += ["", "<b>Dove sta la taglia in più</b>"]
        for e, n in sorted(in_piu, key=lambda x: -esito.contributi.get(x[0], 0)):
            righe.append(f"• {nome_strumento(e)}: ×{1 + n} "
                         f"(ha reso {eur(esito.contributi.get(e, 0.0), True)})")
    else:
        righe.append("Tutte le taglie restano quelle minime: si sale solo con i soldi vinti.")
    if settimanale and esito.contributi:
        righe += ["", "<b>Chi ha reso e chi no, dall'avvio</b>"]
        for e, v in sorted(esito.contributi.items(), key=lambda x: -x[1]):
            righe.append(f"• {nome_strumento(e)}: {eur(v, True)}")
    righe += ["", f"<i>Il quadro completo: {pagina_url()}</i>"]
    return "\n".join(righe)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ["CAPITAL_ENV"] = ENV
    from jobs.account_truth import fetch_transactions
    from src.capital_client import CapitalClient

    solo_stato = "--stato" in sys.argv
    solo_stampa = "--print" in sys.argv or solo_stato
    sali = "--sali" in sys.argv

    es = leggi_esercizio(ENV)
    if not es.get("capitale"):
        log.error("nessun esercizio in corso su %s: la scala non ha una base", ENV)
        return 1
    avvio = str(es["avvio"])
    capitale = float(es["capitale"])

    cfg = load_config()
    cap = CapitalClient(cfg)
    cap.login()
    giorni = max(2, (datetime.now(timezone.utc)
                     - datetime.fromisoformat(avvio)).days + 2)
    tx = fetch_transactions(cap, giorni)
    if not tx:
        log.error("nessuna transazione letta dal broker: non decido nulla")
        return 1

    regole = scalata.Regole()
    st_prec = scalata.leggi(ENV)
    prec = int(st_prec.get("gradino") or 0)
    adesso = datetime.now(ROMA)
    # --sali e' il permesso del cron del lunedi'; la data lo conferma. Senza il
    # flag la salita non c'e' nemmeno di lunedi' (il giro della sera scende e basta).
    puo_salire = sali and scalata.giorno_di_salita(adesso, regole)
    if sali and not puo_salire:
        log.warning("--sali fuori dal giorno di salita (%s): oggi si puo' solo scendere",
                    adesso.strftime("%A"))
    esito = scalata.valuta(tx, avvio, prec, puo_salire, regole,
                           extra_precedente=st_prec.get("extra") or {})

    print(f"gradino {prec} -> {esito.gradino} / {regole.max_gradini} | incassato "
          f"{esito.profitto_netto:+.2f}€ | mancano {esito.mancano:.2f}€ | "
          f"extra {dict((k, v) for k, v in esito.extra.items() if v)}")
    for m in esito.motivi:
        print(f"  · {m}")
    if solo_stato:
        return 0

    scalata.aggiorna_stato(ENV, esito, regole, capitale)
    log.info("scala aggiornata: gradino %d (prima %d), profitto %.2f€",
             esito.gradino, prec, esito.profitto_netto)

    cambiato = esito.gradino != prec
    if not (cambiato or sali):
        return 0
    testo = _messaggio(esito, prec, regole, settimanale=sali)
    if solo_stampa:
        print(re.sub(r"<[^>]+>", "", testo))
        return 0
    from src.telegram_client import TelegramClient
    TelegramClient(cfg).send_message(testo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
