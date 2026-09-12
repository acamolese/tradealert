"""Test della strategia di volatilita'.

Prima cartella di test del progetto: il debito era segnalato nella review del
25 aprile 2026 e mai saldato. Si parte da qui perche' questo e' il sistema che
puo' perdere molto in un giorno solo, quindi e' quello dove un errore silenzioso
costa di piu'.

Stdlib apposta (`unittest`, non pytest): nessuna dipendenza nuova, gira sulla VM
con l'interprete che c'e' gia'.

    python -m unittest discover tests -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.vol_config import carica
from src.vol_segnale import mediana_pendenza
from src.volatilita import (
    Conto, Parametri, Segnale, conto_autorizzato, fine_pausa, in_pausa,
    livello_stop, percentile, perdita_da_salto, scala, serve_ribilancio,
    stop_colpito, unita_target,
)

PAR = Parametri()
# Valori realistici presi dalla storia settimanale Capital 2025-2026, dove la
# mediana del rapporto VIXM/VIX e' 0,79. La pendenza che conta e' quella
# relativa: rapporto grezzo diviso quella mediana.
MEDIANA = 0.79
CALMO = Segnale(vix=17.0, vixm=13.7, percentile=30.0,
                pendenza_mediana=MEDIANA)      # relativa ~1.02
TIEPIDO = Segnale(vix=23.0, vixm=17.1, percentile=55.0,
                  pendenza_mediana=MEDIANA)    # relativa ~0.94
STRESS = Segnale(vix=27.0, vixm=16.6, percentile=90.0,
                 pendenza_mediana=MEDIANA)     # relativa ~0.78, marzo 2026
MATURO = Conto(giorni_operativi=60, risultato_cumulato_eur=5.0,
               giorni_da_ultimo_stop=None, in_pausa=False)


class GuardConto(unittest.TestCase):
    """La regola piu' importante: mai sul conto reale."""

    def test_solo_demo(self):
        self.assertTrue(conto_autorizzato("demo"))
        self.assertTrue(conto_autorizzato(" DEMO "))

    def test_tutto_il_resto_e_vietato(self):
        for env in ("live", "", None, "prod", "reale", "demo2"):
            self.assertFalse(conto_autorizzato(env), env)


class ScalaEsposizione(unittest.TestCase):
    def test_condizioni_piene_danno_il_gradino_pieno(self):
        g = scala(CALMO, MATURO, PAR)
        self.assertEqual(g.nome, "pieno")
        self.assertAlmostEqual(g.frazione, PAR.frazione_piena)

    def test_vix_alto_manda_in_ritirata(self):
        g = scala(Segnale(vix=32.0, vixm=19.0, percentile=95.0,
                          pendenza_mediana=MEDIANA), MATURO, PAR)
        self.assertEqual(g.nome, "ritirata")
        self.assertEqual(g.frazione, 0.0)

    def test_curva_piatta_manda_in_ritirata(self):
        """Marzo 2026: VIX a 27 e curva a 0,78 del suo anno. Il premio si spegne."""
        g = scala(STRESS, MATURO, PAR)
        self.assertEqual(g.nome, "ritirata")
        self.assertEqual(g.frazione, 0.0)

    def test_senza_segnale_si_resta_al_gradino_base(self):
        """Un dato mancante non autorizza mai un aumento, e non chiude nulla."""
        g = scala(Segnale(), MATURO, PAR)
        self.assertEqual(g.nome, "base")
        self.assertAlmostEqual(g.frazione, PAR.frazione_base)

    def test_vix_mancante_non_fa_scattare_la_ritirata(self):
        g = scala(Segnale(vix=None, vixm=18.0, pendenza_mediana=MEDIANA),
                  MATURO, PAR)
        self.assertNotEqual(g.nome, "ritirata")

    def test_senza_mediana_storica_non_si_decide_sulla_curva(self):
        """Il rapporto grezzo da solo non dice niente: senza riferimento si
        resta al gradino base, non si chiude e non si sale."""
        senza = Segnale(vix=17.0, vixm=13.7, percentile=30.0)
        self.assertFalse(senza.completo)
        self.assertEqual(scala(senza, MATURO, PAR).nome, "base")

    def test_sistema_giovane_non_sale(self):
        giovane = Conto(giorni_operativi=5, risultato_cumulato_eur=2.0)
        self.assertEqual(scala(CALMO, giovane, PAR).nome, "base")

    def test_dopo_uno_stop_non_sale(self):
        reduce_ = Conto(giorni_operativi=60, risultato_cumulato_eur=5.0,
                        giorni_da_ultimo_stop=3)
        self.assertEqual(scala(CALMO, reduce_, PAR).nome, "base")

    def test_in_perdita_non_sale(self):
        perdente = Conto(giorni_operativi=60, risultato_cumulato_eur=-4.0)
        self.assertEqual(scala(CALMO, perdente, PAR).nome, "base")

    def test_pausa_batte_tutto(self):
        fermo = Conto(giorni_operativi=90, risultato_cumulato_eur=20.0, in_pausa=True)
        g = scala(CALMO, fermo, PAR)
        self.assertEqual(g.nome, "fermo")
        self.assertEqual(g.frazione, 0.0)

    def test_gradino_intermedio(self):
        """Curva nella norma e VIX medio: si sale, ma non fino in fondo."""
        g = scala(TIEPIDO, MATURO, PAR)
        self.assertEqual(g.nome, "favorevole")
        self.assertAlmostEqual(g.frazione, PAR.frazione_favorevole)

    def test_percentile_alto_frena(self):
        caro = Segnale(vix=17.0, vixm=13.7, percentile=85.0,
                       pendenza_mediana=MEDIANA)
        self.assertEqual(scala(caro, MATURO, PAR).nome, "base")

    def test_nessun_gradino_supera_mai_il_tetto(self):
        stretto = Parametri(tetto=0.10)
        for seg in (CALMO, TIEPIDO, Segnale()):
            self.assertLessEqual(scala(seg, MATURO, stretto).frazione, 0.10)

    def test_ogni_gradino_ha_una_spiegazione(self):
        for seg, conto in ((CALMO, MATURO), (Segnale(), MATURO),
                           (STRESS, MATURO)):
            self.assertTrue(scala(seg, conto, PAR).motivi)


class Taglia(unittest.TestCase):
    def test_unita_arrotondate(self):
        self.assertEqual(unita_target(30.0, 10.0), 3)
        self.assertEqual(unita_target(27.0, 10.0), 3)

    def test_obiettivo_zero_significa_zero(self):
        """Il minimo di una unita' non deve resuscitare una posizione chiusa."""
        self.assertEqual(unita_target(0.0, 10.0), 0)

    def test_obiettivo_piccolo_ma_positivo_da_almeno_una_unita(self):
        self.assertEqual(unita_target(3.0, 10.0), 1)

    def test_nozionale_unitario_assurdo(self):
        self.assertEqual(unita_target(30.0, 0.0), 0)


class Ribilancio(unittest.TestCase):
    def test_prima_apertura_si_fa_sempre(self):
        self.assertTrue(serve_ribilancio(0.0, 30.0, 0.0, PAR))

    def test_scarto_dentro_la_banda_non_si_tocca(self):
        self.assertFalse(serve_ribilancio(33.0, 30.0, -3.0, PAR))

    def test_scarto_oltre_la_banda_si_ritocca(self):
        self.assertTrue(serve_ribilancio(60.0, 30.0, -6.0, PAR))

    def test_uscita_completa_con_posizione_aperta(self):
        self.assertTrue(serve_ribilancio(30.0, 0.0, -3.0, PAR))

    def test_uscita_completa_senza_posizione_non_fa_nulla(self):
        self.assertFalse(serve_ribilancio(0.0, 0.0, 0.0, PAR))


class StopEProtezioni(unittest.TestCase):
    def test_stop_broker_corrisponde_al_limite_in_euro(self):
        """Con 30 € di esposizione e 15 € di limite, lo stop sta al +50%."""
        self.assertAlmostEqual(livello_stop(10.0, 30.0, PAR), 15.0, places=2)

    def test_stop_broker_non_supera_la_distanza_massima(self):
        self.assertAlmostEqual(livello_stop(10.0, 5.0, PAR), 16.0, places=2)

    def test_niente_stop_senza_esposizione(self):
        self.assertIsNone(livello_stop(10.0, 0.0, PAR))

    def test_kill_switch_scatta_oltre_il_limite(self):
        self.assertTrue(stop_colpito(-15.0, -3.0, PAR))
        self.assertTrue(stop_colpito(-20.0, -3.0, PAR))

    def test_kill_switch_non_scatta_sotto_il_limite(self):
        self.assertFalse(stop_colpito(-14.99, -3.0, PAR))

    def test_kill_switch_non_scatta_senza_posizione(self):
        """Senza posizione aperta non c'e' niente da chiudere."""
        self.assertFalse(stop_colpito(-99.0, 0.0, PAR))

    def test_pausa_attiva_e_scaduta(self):
        adesso = datetime.now(timezone.utc)
        futuro = {"in_pausa_fino": (adesso + timedelta(days=2)).isoformat()}
        passato = {"in_pausa_fino": (adesso - timedelta(days=2)).isoformat()}
        self.assertTrue(in_pausa(futuro, adesso))
        self.assertFalse(in_pausa(passato, adesso))
        self.assertFalse(in_pausa({}, adesso))

    def test_fine_pausa_rispetta_i_giorni_configurati(self):
        adesso = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.assertTrue(fine_pausa(adesso, PAR).startswith("2026-09-17"))

    def test_perdita_da_salto(self):
        """Il numero che va scritto ogni volta che l'esposizione sale."""
        self.assertAlmostEqual(perdita_da_salto(30.0), 19.8)
        self.assertAlmostEqual(perdita_da_salto(50.0), 33.0)


class PendenzaRelativa(unittest.TestCase):
    """La parte che il primo giro dal vivo ha smascherato: VIXM e' un ETF, il
    suo prezzo non e' confrontabile con il livello del VIX."""

    def test_relativa_confronta_con_la_propria_storia(self):
        s = Segnale(vix=17.7, vixm=13.2, pendenza_mediana=0.79)
        self.assertAlmostEqual(s.pendenza, 0.7458, places=3)
        self.assertAlmostEqual(s.pendenza_relativa, 0.944, places=2)

    def test_relativa_assente_senza_mediana(self):
        self.assertIsNone(Segnale(vix=17.7, vixm=13.2).pendenza_relativa)

    def test_mediana_richiede_abbastanza_settimane(self):
        spot = {f"2026-01-{g:02d}": 18.0 for g in range(1, 10)}
        medio = {f"2026-01-{g:02d}": 14.0 for g in range(1, 10)}
        self.assertIsNone(mediana_pendenza(spot, medio))

    def test_mediana_usa_solo_le_date_in_comune(self):
        spot = {f"2026-01-{g:02d}": 20.0 for g in range(1, 26)}
        medio = {f"2026-01-{g:02d}": 15.0 for g in range(1, 26)}
        medio["2026-02-01"] = 99.0          # data che lo spot non ha
        self.assertAlmostEqual(mediana_pendenza(spot, medio), 0.75)


class Percentile(unittest.TestCase):
    def test_posizione_nella_storia(self):
        storia = [float(v) for v in range(1, 101)]
        self.assertAlmostEqual(percentile(50.0, storia), 49.0)

    def test_storia_troppo_corta(self):
        self.assertIsNone(percentile(20.0, [15.0, 16.0]))


class Configurazione(unittest.TestCase):
    def _scrivi(self, contenuto: dict) -> Path:
        f = Path(tempfile.mkdtemp()) / "volatilita.json"
        f.write_text(json.dumps(contenuto))
        return f

    def test_file_versionato_e_valido(self):
        par = carica(env={})
        self.assertEqual(par.epic, "UVXY")
        self.assertLessEqual(par.frazione_piena, par.tetto)

    def test_il_file_vince_sui_default(self):
        par = carica(self._scrivi({"stop_eur": 9.0}), env={})
        self.assertEqual(par.stop_eur, 9.0)

    def test_le_variabili_legacy_vincono_sul_file(self):
        """La VM deve poter correggere un parametro senza un deploy."""
        par = carica(self._scrivi({"stop_eur": 9.0}),
                     env={"PAURA_STOP_EUR": "4.0"})
        self.assertEqual(par.stop_eur, 4.0)

    def test_chiave_sconosciuta_ignorata(self):
        par = carica(self._scrivi({"colore": "rosso", "stop_eur": 8.0}), env={})
        self.assertEqual(par.stop_eur, 8.0)

    def test_file_rotto_non_ferma_il_sistema(self):
        f = Path(tempfile.mkdtemp()) / "rotto.json"
        f.write_text("{ questo non e' json")
        self.assertEqual(carica(f, env={}).epic, Parametri().epic)

    def test_file_assente_non_ferma_il_sistema(self):
        assente = Path(tempfile.mkdtemp()) / "manca.json"
        self.assertEqual(carica(assente, env={}).stop_eur, Parametri().stop_eur)

    def test_il_tetto_vince_su_un_gradino_distratto(self):
        """Chi alza un gradino e dimentica il tetto non aumenta il rischio."""
        par = carica(self._scrivi({"frazione_piena": 0.9, "tetto": 0.25}), env={})
        self.assertEqual(par.frazione_piena, 0.25)

    def test_valore_non_numerico_ignorato(self):
        par = carica(self._scrivi({"stop_eur": "tanti"}), env={})
        self.assertEqual(par.stop_eur, Parametri().stop_eur)


if __name__ == "__main__":
    unittest.main()
