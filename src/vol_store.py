"""Registra su Supabase quello che la strategia di volatilita' decide e fa.

Principio che vale per tutto il modulo: **la persistenza non deve mai fermare il
trading, e il trading non deve mai dipendere dalla persistenza**. Se Supabase non
risponde si logga l'errore e si prosegue, perche' una posizione corta su uno
strumento che puo' salire del 66% in un giorno non puo' restare in sospeso in
attesa di una insert.

Il rovescio della medaglia e' dichiarato: quando il DB e' muto, quel run non
esiste nelle analisi. Il log resta l'unica traccia.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.volatilita import Conto, Gradino, Segnale

log = logging.getLogger(__name__)

SCHEMA = "vol"


class VolStore:
    """Accesso alle tabelle dello schema `vol`. Nessun metodo solleva."""

    def __init__(self, db: Any | None) -> None:
        self._sp = None
        if db is None:
            return
        try:
            self._sp = db._client.schema(SCHEMA)
        except Exception as exc:   # client assente o schema non esposto
            log.error("schema %s non raggiungibile: %s", SCHEMA, exc)

    @property
    def attivo(self) -> bool:
        return self._sp is not None

    # --- scritture -------------------------------------------------------

    def decisione(self, *, epic: str, gradino: Gradino, azione: str,
                  eseguito: bool, segnale: Segnale, conto: Conto,
                  capitale: float | None = None,
                  nozionale_attuale: float | None = None,
                  nozionale_target: float | None = None,
                  size_prima: float | None = None, size_dopo: float | None = None,
                  risultato_eur: float | None = None,
                  stop_level: float | None = None) -> None:
        """Una riga per ogni run, anche quando il sistema non tocca nulla.

        Le decisioni di non fare niente sono dati quanto le altre: senza di esse
        non si puo' ricostruire per quanti giorni una condizione ha tenuto fermo
        il sistema.
        """
        riga = {
            "epic": epic,
            "gradino": gradino.nome,
            "frazione_target": gradino.frazione,
            "capitale_eur": capitale,
            "nozionale_attuale_eur": nozionale_attuale,
            "nozionale_target_eur": nozionale_target,
            "size_prima": size_prima,
            "size_dopo": size_dopo,
            "azione": azione,
            "eseguito": eseguito,
            "motivo": "; ".join(gradino.motivi) if gradino.motivi else None,
            "risultato_eur": risultato_eur,
            "stop_level": stop_level,
            "features_at_decision": features_decisione(segnale, conto),
        }
        self._inserisci("decisione", riga)

    def apertura(self, *, epic: str, deal_id: str | None, verso: str,
                 size: float, prezzo: float | None) -> None:
        self._inserisci("posizione", {
            "epic": epic, "deal_id": deal_id, "verso": verso,
            "size": size, "prezzo_apertura": prezzo,
        })

    def chiusura(self, *, epic: str, prezzo: float | None,
                 risultato_eur: float | None, motivo: str) -> int:
        """Marca chiuse le posizioni ancora aperte su quell'epic. Ritorna quante."""
        if not self._sp:
            return 0
        try:
            aperte = (self._sp.table("posizione").select("id")
                      .eq("epic", epic).is_("chiusa_il", "null").execute()).data or []
            if not aperte:
                return 0
            self._sp.table("posizione").update({
                "chiusa_il": datetime.now(timezone.utc).isoformat(),
                "prezzo_chiusura": prezzo,
                "risultato_eur": risultato_eur,
                "motivo_chiusura": motivo,
            }).in_("id", [r["id"] for r in aperte]).execute()
            return len(aperte)
        except Exception as exc:
            log.error("chiusura non registrata (%s): %s", motivo, exc)
            return 0

    def shadow(self, riga: dict[str, Any]) -> None:
        """Misura giornaliera delle varianti. Una riga al giorno, riscrivibile."""
        if not self._sp:
            return
        try:
            self._sp.table("segnale_shadow").upsert(
                riga, on_conflict="giorno").execute()
        except Exception as exc:
            log.error("segnale shadow non registrato: %s", exc)

    # --- letture ---------------------------------------------------------

    def posizioni_aperte(self, epic: str) -> list[dict[str, Any]]:
        if not self._sp:
            return []
        try:
            return (self._sp.table("posizione").select("*")
                    .eq("epic", epic).is_("chiusa_il", "null").execute()).data or []
        except Exception as exc:
            log.error("lettura posizioni aperte fallita: %s", exc)
            return []

    def giorni_shadow(self) -> int:
        if not self._sp:
            return 0
        try:
            r = (self._sp.table("segnale_shadow").select("giorno", count="exact")
                 .limit(1).execute())
            return int(r.count or 0)
        except Exception as exc:
            log.error("conteggio shadow fallito: %s", exc)
            return 0

    # --- interno ---------------------------------------------------------

    def _inserisci(self, tabella: str, riga: dict[str, Any]) -> None:
        if not self._sp:
            return
        try:
            self._sp.table(tabella).insert(riga).execute()
        except Exception as exc:
            log.error("insert su %s.%s fallita: %s", SCHEMA, tabella, exc)


def features_decisione(segnale: Segnale, conto: Conto) -> dict[str, Any]:
    """Lo stato che ha prodotto la decisione, per le analisi retrospettive."""
    return {
        "vix": segnale.vix,
        "vixm": segnale.vixm,
        "pendenza": segnale.pendenza,
        "percentile_vix": segnale.percentile,
        "segnale_completo": segnale.completo,
        "giorni_operativi": conto.giorni_operativi,
        "risultato_cumulato_eur": conto.risultato_cumulato_eur,
        "giorni_da_ultimo_stop": conto.giorni_da_ultimo_stop,
        "in_pausa": conto.in_pausa,
    }
