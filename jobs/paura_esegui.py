"""Vende assicurazione sulla volatilita': apre e mantiene la posizione.

Cosa fa, in una riga: sta corto su uno strumento costruito per perdere valore
(UVXY rinnova ogni giorno contratti a termine piu' cari di quelli che scadono,
-79,8% l'anno su quindici anni) e incassa quel decadimento.

Non prevede niente. Il rendimento e' il premio di chi vende protezione: si
guadagna poco quasi sempre e si perde molto raramente. Il 5 febbraio 2018 UVXY
e' salito del 66% in una seduta, e con l'esposizione qui prevista sarebbe stata
una perdita del 10% del capitale in un giorno.

L'unica cosa che il sistema modula e' QUANTA esposizione tenere: un gradino fra
zero e il tetto, scelto su condizioni verificabili (pendenza della curva, livello
del VIX, storia del conto) e registrato per intero su Supabase. La logica sta in
`src/volatilita.py`, i parametri in `config/volatilita.json`.

Sicurezze: solo sul conto di prova, mai a mercato chiuso, tetto sull'esposizione,
stop di emergenza con pausa obbligatoria, ritirata quando la curva si inverte.

Uso:
  python -m jobs.paura_esegui             # apre, mantiene, ribilancia
  python -m jobs.paura_esegui --stato     # guarda e basta
  python -m jobs.paura_esegui --chiudi    # chiude tutto e si ferma
"""
from __future__ import annotations

import logging
import sys

from src.vol_runner import esegui


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return esegui(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
