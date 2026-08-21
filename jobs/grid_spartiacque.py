"""Report SPARTIACQUE: chiude i conti col vecchio sistema e apre i nuovi.

Serve a fissare per iscritto, su Telegram, il confine tra la configurazione
sbagliata (ancoraggio fisso, dal 19/08 al 21/08) e quella corretta (ancoraggio
mobile EMA5 + passo 3%, dal 21/08 13:31). Senza uno spartiacque esplicito i
numeri delle due fasi si mescolano e diventa impossibile giudicare la seconda.

Uso: python -m jobs.grid_spartiacque [--print]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.config import load_config

# istante del cambio: prima apertura del sistema corretto.
# In UTC perche' si confronta con dateUtc del broker; sono le 13:31 italiane.
TAGLIO = "2026-08-21T11:31"


def raccogli(env: str, nome: str) -> dict:
    from src.capital_client import CapitalClient
    from jobs.account_truth import fetch_transactions, _amount

    os.environ["CAPITAL_ENV"] = env
    cap = CapitalClient(load_config())
    cap.login()
    bal = (cap.get_account_info().get("accounts") or [{}])[0].get("balance") or {}
    equity = float(bal.get("balance") or 0) + float(bal.get("profitLoss") or 0)

    tx = fetch_transactions(cap, 4)
    tr = [t for t in tx if t.get("transactionType") == "TRADE"]
    vecchi = [t for t in tr if (t.get("dateUtc") or "") < TAGLIO]
    nuovi = [t for t in tr if (t.get("dateUtc") or "") >= TAGLIO]
    pos = cap.get_open_positions()
    return {
        "nome": nome, "equity": equity,
        "v_n": len(vecchi), "v_pnl": sum(_amount(t) for t in vecchi),
        "n_n": len(nuovi), "n_pnl": sum(_amount(t) for t in nuovi),
        "interessi": sum(_amount(t) for t in tx if t.get("transactionType") == "SWAP"),
        "posizioni": len(pos),
    }


def main() -> int:
    solo_stampa = "--print" in sys.argv
    originale = os.environ.get("CAPITAL_ENV")
    conti = [raccogli("live", "REALE"), raccogli("demo", "DEMO")]
    if originale is None:
        os.environ.pop("CAPITAL_ENV", None)
    else:
        os.environ["CAPITAL_ENV"] = originale

    righe = []
    for c in conti:
        righe.append(
            f"<b>{c['nome']}</b> — ora {c['equity']:.2f}€\n"
            f"   vecchio: {c['v_n']} mosse, <b>{c['v_pnl']:+.2f}€</b>\n"
            f"   nuovo: {c['n_n']} mosse, {c['n_pnl']:+.2f}€\n"
            f"   posizioni aperte: {c['posizioni']}")

    testo = (
        "🔻 <b>SPARTIACQUE — fine del vecchio sistema</b>\n"
        f"<i>{datetime.now(ZoneInfo('Europe/Rome')).strftime('%d/%m/%Y %H:%M')}</i>\n\n"
        "<b>COSA NON ANDAVA</b>\n"
        "Il grid teneva il punto di riferimento fisso al prezzo di partenza. "
        "Su un mercato che sale e non torna indietro vendeva a ogni gradino fino "
        "al tetto e lì restava bloccato, scoperto contro il mercato.\n"
        "Misurato su 400 giorni: oro venduto il 98,5% del tempo mentre saliva del "
        "42%, Nasdaq 98,5%, S&amp;P 97,8%. Correlazione -0,97: più un mercato "
        "saliva, più il sistema ci stava contro.\n\n"
        "<b>BILANCIO DEL VECCHIO</b>\n" + "\n\n".join(righe) + "\n\n"
        "<b>COSA È CAMBIATO</b>\n"
        "• il riferimento ora <b>segue il mercato</b> (media mobile) invece di "
        "restare fermo\n"
        "• passo da 0,2% a <b>3%</b>: servivano entrambe le cose insieme\n"
        "• leva da <b>11x a 2,5x</b>\n"
        "• aggiunto lo <b>stop sulle perdite</b> (mancava del tutto: a +20€ si "
        "fermava, a -11€ no)\n"
        "• corretto il report che mostrava numeri falsi (+0,49€ mentre perdeva "
        "5,66€)\n\n"
        "<b>VERIFICA PRIMA DI RIPARTIRE</b>\n"
        "120 finestre storiche su 6 strumenti:\n"
        "   vecchio: 20% positive, caso peggiore -343%\n"
        "   nuovo: <b>82% positive</b>, caso peggiore -42%\n\n"
        "<i>Da adesso i numeri ripartono da zero. Tutto ciò che vedrai nel "
        "riepilogo orario è prodotto dal sistema corretto.</i>")

    if solo_stampa:
        import re
        print(re.sub(r"<[^>]+>", "", testo))
        return 0
    from src.telegram_client import TelegramClient
    TelegramClient(load_config()).send_message(testo)
    print("inviato")
    return 0


if __name__ == "__main__":
    sys.exit(main())
