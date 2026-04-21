"""Client minimale per Capital.com REST API.

Gestisce autenticazione (CST + X-SECURITY-TOKEN), fetch di info account,
metadata strumenti, prezzi snapshot e candele storiche. Le operazioni di
trading vere (apertura/chiusura posizioni) saranno aggiunte in fase 2.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .config import Config

log = logging.getLogger(__name__)


class CapitalAPIError(Exception):
    """Errore Capital.com con corpo della risposta."""

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"Capital {status} on {url}: {body}")
        self.status = status
        self.body = body
        self.url = url


def _raise_for_status(response: requests.Response) -> None:
    if response.ok:
        return
    body = response.text[:500]
    log.error(
        "Capital %s on %s: %s", response.status_code, response.url, body
    )
    raise CapitalAPIError(response.status_code, body, response.url)


class CapitalClient:
    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._session = requests.Session()
        self._cst: str | None = None
        self._security_token: str | None = None

    def _url(self, path: str) -> str:
        return f"{self._cfg.capital_base_url}{path}"

    def _auth_headers(self) -> dict[str, str]:
        if not self._cst or not self._security_token:
            raise RuntimeError("Client non autenticato: chiama login() prima.")
        return {
            "X-CAP-API-KEY": self._cfg.capital_api_key,
            "CST": self._cst,
            "X-SECURITY-TOKEN": self._security_token,
            "Content-Type": "application/json",
        }

    def login(self, max_retries: int = 3) -> None:
        """Autentica e memorizza CST + security token nei session header.
        Retry con backoff esponenziale su 429 (rate limit, ~1 req/sec
        ammesso da Capital) e su errori di rete transitori (connect/read
        timeout, ConnectionError) per assorbire i blip dell'uplink."""
        import time

        for attempt in range(max_retries):
            try:
                response = self._session.post(
                    self._url("/session"),
                    headers={
                        "X-CAP-API-KEY": self._cfg.capital_api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "identifier": self._cfg.capital_identifier,
                        "password": self._cfg.capital_password,
                    },
                    timeout=15,
                )
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                if attempt >= max_retries - 1:
                    raise
                wait = 2 * (2**attempt)  # 2s, 4s, 8s
                log.warning(
                    "Login Capital: errore di rete (%s), retry %d/%d tra %ds",
                    type(exc).__name__,
                    attempt + 1,
                    max_retries - 1,
                    wait,
                )
                time.sleep(wait)
                continue
            if response.status_code == 429 and attempt < max_retries - 1:
                wait = 5 * (2**attempt)  # 5s, 10s, 20s
                time.sleep(wait)
                continue
            _raise_for_status(response)
            self._cst = response.headers.get("CST")
            self._security_token = response.headers.get("X-SECURITY-TOKEN")
            if not self._cst or not self._security_token:
                raise RuntimeError(
                    "Login Capital.com riuscito ma mancano i token di sessione."
                )
            return

    def get_account_info(self) -> dict[str, Any]:
        response = self._session.get(
            self._url("/accounts"),
            headers=self._auth_headers(),
            timeout=15,
        )
        _raise_for_status(response)
        return response.json()

    def search_market(self, search_term: str) -> list[dict[str, Any]]:
        """Cerca strumenti per nome o ticker. Utile per trovare gli epic."""
        response = self._session.get(
            self._url("/markets"),
            headers=self._auth_headers(),
            params={"searchTerm": search_term},
            timeout=15,
        )
        _raise_for_status(response)
        return response.json().get("markets", [])

    def get_market(self, epic: str) -> dict[str, Any]:
        """Restituisce snapshot completo per un singolo epic, incluso bid/offer."""
        response = self._session.get(
            self._url(f"/markets/{epic}"),
            headers=self._auth_headers(),
            timeout=15,
        )
        _raise_for_status(response)
        return response.json()

    def get_market_navigation(
        self, node_id: str | None = None
    ) -> dict[str, Any]:
        """Naviga la gerarchia mercati Capital.com. Senza ``node_id``
        ritorna i gruppi top-level (crypto, shares, forex, ecc.).
        Con un ``node_id`` ritorna ``nodes`` (sotto-categorie) e ``markets``
        (foglie con metadati: epic, percentageChange, bid/offer,
        marketStatus, instrumentType). Utile per la discovery dinamica
        senza hardcoded watchlist."""
        path = "/marketnavigation"
        if node_id:
            path = f"{path}/{node_id}"
        response = self._session.get(
            self._url(path),
            headers=self._auth_headers(),
            timeout=15,
        )
        _raise_for_status(response)
        return response.json()

    def get_prices(
        self,
        epic: str,
        resolution: str = "HOUR",
        max_bars: int = 100,
    ) -> list[dict[str, Any]]:
        """Candele storiche per un epic.

        Resolution accettate: MINUTE, MINUTE_5, MINUTE_15, MINUTE_30,
        HOUR, HOUR_4, DAY, WEEK.
        """
        response = self._session.get(
            self._url(f"/prices/{epic}"),
            headers=self._auth_headers(),
            params={"resolution": resolution, "max": max_bars},
            timeout=20,
        )
        _raise_for_status(response)
        return response.json().get("prices", [])

    # ---------- Trading ----------

    def get_open_positions(self) -> list[dict[str, Any]]:
        response = self._session.get(
            self._url("/positions"),
            headers=self._auth_headers(),
            timeout=15,
        )
        _raise_for_status(response)
        return response.json().get("positions", [])

    def create_position(
        self,
        epic: str,
        direction: str,  # "BUY" o "SELL"
        size: float,
        stop_level: float | None = None,
        profit_level: float | None = None,
        guaranteed_stop: bool = False,
    ) -> dict[str, Any]:
        """Apre una posizione di mercato. Restituisce il dealReference.

        Stop e take profit sono opzionali ma in produzione sono passati sempre.
        """
        body: dict[str, Any] = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": guaranteed_stop,
            "forceOpen": True,
        }
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level

        response = self._session.post(
            self._url("/positions"),
            headers=self._auth_headers(),
            json=body,
            timeout=20,
        )
        _raise_for_status(response)
        return response.json()

    def confirm_deal(
        self,
        deal_reference: str,
        retries: int = 5,
        backoff_sec: float = 0.6,
    ) -> dict[str, Any]:
        """Conferma esito di un'operazione asincrona via dealReference.

        Capital ha un piccolo lag tra create_position/close_position e
        l'effettiva disponibilita' del dealReference su /confirms.
        Riproviamo qualche volta su 404 prima di sollevare.
        """
        import time

        last_exc: CapitalAPIError | None = None
        for attempt in range(retries):
            response = self._session.get(
                self._url(f"/confirms/{deal_reference}"),
                headers=self._auth_headers(),
                timeout=15,
            )
            if response.ok:
                return response.json()
            if response.status_code != 404:
                _raise_for_status(response)
            # 404: il deal non e' ancora indicizzato, attendi e riprova
            last_exc = CapitalAPIError(
                response.status_code, response.text[:500], response.url
            )
            time.sleep(backoff_sec * (attempt + 1))
        if last_exc:
            raise last_exc
        raise RuntimeError("confirm_deal: nessuna risposta dopo i retry")

    def close_position(self, deal_id: str) -> dict[str, Any]:
        response = self._session.delete(
            self._url(f"/positions/{deal_id}"),
            headers=self._auth_headers(),
            timeout=15,
        )
        _raise_for_status(response)
        return response.json()

    def get_activity_history(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        last_period_sec: int = 24 * 3600,
        detailed: bool = True,
    ) -> list[dict[str, Any]]:
        """History delle attivita' account (aperture, chiusure, modifiche SL).

        Se ``from_date`` e ``to_date`` sono forniti (ISO-8601), ha la
        precedenza sul ``last_period_sec``. Il flag ``detailed`` include i
        dettagli del deal (incl. level di chiusura e amount P&L).
        """
        params: dict[str, Any] = {"detailed": "true" if detailed else "false"}
        if from_date and to_date:
            params["from"] = from_date
            params["to"] = to_date
        else:
            params["lastPeriod"] = last_period_sec
        response = self._session.get(
            self._url("/history/activity"),
            headers=self._auth_headers(),
            params=params,
            timeout=20,
        )
        _raise_for_status(response)
        return response.json().get("activities", [])

    def update_position(
        self,
        deal_id: str,
        stop_level: float | None = None,
        profit_level: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        response = self._session.put(
            self._url(f"/positions/{deal_id}"),
            headers=self._auth_headers(),
            json=body,
            timeout=15,
        )
        _raise_for_status(response)
        return response.json()
