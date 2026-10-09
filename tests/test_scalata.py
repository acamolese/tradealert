"""La scala: si sale solo con i soldi vinti, il lunedi'; si scende subito."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src import scalata
from src.scalata import (Regole, assegna, contributi_da_transazioni,
                         giorno_di_salita, gradino_da_profitto,
                         gradino_effettivo, riduci, valuta)

AVVIO = "2026-09-07T05:27:41+00:00"


def tx(nome, tipo, size, quando="2026-10-01T10:00:00"):
    return {"instrumentName": nome, "transactionType": tipo, "size": str(size),
            "dateUtc": quando}


class Misura(unittest.TestCase):
    def test_conta_operazioni_costi_e_dividendi(self):
        c = contributi_da_transazioni([tx("US500", "TRADE", 1.5), tx("US500", "SWAP", -0.2),
                                       tx("US500", "CORPORATE_ACTION", 0.05)], AVVIO)
        self.assertAlmostEqual(c["US500"], 1.35)

    def test_ignora_quello_che_precede_l_avvio(self):
        c = contributi_da_transazioni([tx("US500", "TRADE", 9.0, "2026-09-01T00:00:00"),
                                       tx("US500", "TRADE", 1.0)], AVVIO)
        self.assertEqual(c, {"US500": 1.0})

    def test_ignora_tipi_sconosciuti_e_importi_rotti(self):
        c = contributi_da_transazioni([tx("US500", "DEPOSIT", 500), tx("US500", "TRADE", "x"),
                                       tx("US500", "TRADE", 0.4)], AVVIO)
        self.assertEqual(c, {"US500": 0.4})

    def test_nessuna_transazione(self):
        self.assertEqual(contributi_da_transazioni([], AVVIO), {})


class Gradini(unittest.TestCase):
    def test_un_gradino_ogni_passo(self):
        r = Regole(passo_eur=5.0)
        self.assertEqual(gradino_da_profitto(4.99, r), 0)
        self.assertEqual(gradino_da_profitto(5.0, r), 1)
        self.assertEqual(gradino_da_profitto(12.0, r), 2)

    def test_perdita_vale_zero_mai_negativo(self):
        self.assertEqual(gradino_da_profitto(-30.0, Regole()), 0)

    def test_non_oltre_la_scala(self):
        r = Regole()
        self.assertEqual(gradino_da_profitto(10_000.0, r), r.max_gradini)

    def test_si_scende_sempre(self):
        self.assertEqual(gradino_effettivo(3, 1, puo_salire=False), 1)
        self.assertEqual(gradino_effettivo(3, 1, puo_salire=True), 1)

    def test_si_sale_solo_col_permesso(self):
        self.assertEqual(gradino_effettivo(1, 3, puo_salire=False), 1)
        self.assertEqual(gradino_effettivo(1, 3, puo_salire=True), 3)

    def test_il_giorno_di_salita_e_lunedi(self):
        self.assertTrue(giorno_di_salita(date(2026, 10, 12), Regole()))   # lunedi'
        self.assertFalse(giorno_di_salita(date(2026, 10, 9), Regole()))   # venerdi'


class Assegnazione(unittest.TestCase):
    def test_il_gradino_va_a_chi_ha_reso_di_piu(self):
        extra = assegna(1, {"US500": 1.3, "J225": 1.5, "GOLD": -0.7}, Regole())
        self.assertEqual(extra["J225"], 1)
        self.assertEqual(sum(extra.values()), 1)

    def test_chi_perde_non_riceve_gradini(self):
        extra = assegna(3, {"US500": 1.3, "GOLD": -0.7}, Regole())
        self.assertEqual(extra["GOLD"], 0)
        self.assertEqual(sum(extra.values()), 1)   # un solo strumento in utile

    def test_una_taglia_in_piu_per_strumento_al_massimo(self):
        extra = assegna(5, {"US500": 9.0, "J225": 1.0}, Regole(max_extra_per_slot=1))
        self.assertEqual(extra, {**{e: 0 for e in Regole().slot}, "US500": 1, "J225": 1})

    def test_zero_gradini_zero_extra(self):
        self.assertEqual(sum(assegna(0, {"US500": 50.0}, Regole()).values()), 0)

    def test_ridurre_toglie_a_chi_ha_reso_meno(self):
        prima = {"US500": 1, "J225": 1, "GOLD": 1}
        dopo = riduci(prima, 1, {"US500": 3.0, "J225": 2.0, "GOLD": 0.5}, Regole())
        self.assertEqual(dopo["GOLD"], 0)
        self.assertEqual(dopo["US500"], 1)
        self.assertEqual(dopo["J225"], 1)


class GiroCompleto(unittest.TestCase):
    def _tx(self, us500=6.0, j225=5.5, gold=-1.0):
        return [tx("US500", "TRADE", us500), tx("J225", "TRADE", j225),
                tx("GOLD", "TRADE", gold)]

    def test_lunedi_sale_e_distribuisce(self):
        e = valuta(self._tx(), AVVIO, precedente=0, puo_salire=True)
        self.assertAlmostEqual(e.profitto_netto, 10.5)
        self.assertEqual(e.gradino, 2)
        self.assertEqual(e.extra["US500"], 1)
        self.assertEqual(e.extra["J225"], 1)
        self.assertEqual(e.extra["GOLD"], 0)
        self.assertAlmostEqual(e.mancano, 4.5)
        self.assertTrue(any("salgo" in m for m in e.motivi))

    def test_sera_feriale_non_sale(self):
        e = valuta(self._tx(), AVVIO, precedente=0, puo_salire=False)
        self.assertEqual(e.gradino, 0)
        self.assertEqual(sum(e.extra.values()), 0)
        self.assertTrue(any("lunedì" in m for m in e.motivi))

    def test_sera_feriale_scende_subito_e_toglie_al_peggiore(self):
        prec_extra = {"US500": 1, "J225": 1}
        e = valuta(self._tx(us500=6.0, j225=0.2, gold=-1.0), AVVIO, precedente=2,
                   puo_salire=False, extra_precedente=prec_extra)
        self.assertEqual(e.gradino, 1)          # 5,2 € coprono un gradino solo
        self.assertEqual(e.extra["US500"], 1)
        self.assertEqual(e.extra["J225"], 0)

    def test_fra_un_lunedi_e_l_altro_la_distribuzione_non_si_muove(self):
        prec_extra = {"US500": 1, "J225": 1}
        # J225 ha superato US500, ma non e' lunedi': nessuno spostamento
        e = valuta(self._tx(us500=5.0, j225=9.0), AVVIO, precedente=2,
                   puo_salire=False, extra_precedente=prec_extra)
        self.assertEqual(e.gradino, 2)
        self.assertEqual(e.extra, {**{s: 0 for s in Regole().slot}, "US500": 1, "J225": 1})

    def test_in_perdita_la_scala_e_a_zero(self):
        e = valuta(self._tx(us500=-2.0, j225=-1.0, gold=-1.0), AVVIO, precedente=3,
                   puo_salire=True, extra_precedente={"US500": 1, "J225": 1, "GOLD": 1})
        self.assertEqual(e.gradino, 0)
        self.assertEqual(sum(e.extra.values()), 0)
        self.assertAlmostEqual(e.mancano, 9.0)

    def test_i_gradini_senza_strumento_in_utile_non_si_usano(self):
        # 20 € di profitto, ma un solo strumento in utile: un solo gradino
        e = valuta([tx("US500", "TRADE", 20.0)], AVVIO, precedente=0, puo_salire=True)
        self.assertEqual(e.gradino, 1)

    def test_scala_piena(self):
        r = Regole()
        t = [tx(s, "TRADE", 10.0) for s in r.slot]
        e = valuta(t, AVVIO, precedente=0, puo_salire=True, regole=r)
        self.assertEqual(e.gradino, r.max_gradini)
        self.assertEqual(e.mancano, 0.0)


class StatoSuFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._p = mock.patch.object(scalata, "DATA", self.dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_senza_file_la_taglia_e_quella_minima(self):
        self.assertEqual(scalata.moltiplicatore("US500", "demo"), 1)
        self.assertEqual(scalata.extra_per("SVXY", "demo"), 0)

    def test_file_rotto_vale_zero(self):
        (self.dir / "scalata_demo.json").write_text("{ rotto")
        self.assertEqual(scalata.moltiplicatore("US500", "demo"), 1)

    def test_valore_negativo_non_riduce_mai(self):
        (self.dir / "scalata_demo.json").write_text(json.dumps({"extra": {"US500": -3}}))
        self.assertEqual(scalata.moltiplicatore("US500", "demo"), 1)

    def test_aggiorna_stato_tiene_la_storia(self):
        e = valuta([tx("US500", "TRADE", 6.0)], AVVIO, 0, True)
        st = scalata.aggiorna_stato("demo", e, Regole(), 200.0)
        self.assertEqual(st["gradino"], 1)
        self.assertEqual(scalata.moltiplicatore("US500", "demo"), 2)
        self.assertEqual(len(st["storia"]), 1)
        e2 = valuta([tx("US500", "TRADE", 6.0)], AVVIO, 1, False, extra_precedente=st["extra"])
        st2 = scalata.aggiorna_stato("demo", e2, Regole(), 200.0)
        self.assertEqual(len(st2["storia"]), 1)     # niente cambio, niente riga


if __name__ == "__main__":
    unittest.main()
