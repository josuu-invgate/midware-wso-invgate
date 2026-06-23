"""
Cliente de InvGate Asset Management (Public API, JSON:API).

Responsabilidades:
  - Obtener y renovar el token OAuth2 (client_credentials).
  - Buscar un asset por serial (para deduplicar antes de crear).
  - Crear (POST) y actualizar (PATCH) assets vía /public-api/assets-lite/.

Logging: cada request/respuesta se loguea a nivel DEBUG (logger "invgate"),
con el token Bearer enmascarado y SIN loguear nunca client_id/client_secret.
Así, en el archivo de log, podés ver el body JSON exacto que se manda a InvGate.

Docs de referencia: colección Postman "IGAM" (InvGate Asset Management).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from config import IGAMConfig

# Content-Type obligatorio para escribir en la Public API (spec JSON:API).
JSON_API = "application/vnd.api+json"

logger = logging.getLogger("invgate")

_MAX_LOG_BODY = 6000  # truncado de cuerpos largos en el log


class IGAMError(RuntimeError):
    """Error devuelto por la API de InvGate."""


def _extract_list(payload: Any) -> List[Dict[str, Any]]:
    """Extrae la lista de items soportando {data:[...]}, {results:[...]} o [...] pelado."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _mask_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    safe = dict(headers or {})
    if "Authorization" in safe:
        val = safe["Authorization"]
        safe["Authorization"] = (val[:14] + "…(enmascarado)") if val else val
    return safe


class IGAMClient:
    def __init__(self, cfg: IGAMConfig, timeout: int = 30, verify_tls: bool = True):
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._person_cache: Dict[str, Optional[str]] = {}
        self._location_cache: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------ http
    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        auth=None,
        log_request_body: bool = True,
        redact_response: bool = False,
    ):
        """Envía un request logueando todo (request + response) a nivel DEBUG."""
        logger.debug("→ %s %s", method.upper(), url)
        if params:
            logger.debug("   params: %s", params)
        logger.debug("   headers: %s", _mask_headers(headers))
        if json_body is not None and log_request_body:
            logger.debug("   request body:\n%s", json.dumps(json_body, ensure_ascii=False, indent=2))

        resp = self.session.request(
            method, url, params=params, json=json_body, data=data,
            headers=headers, auth=auth, timeout=self.timeout,
        )

        if redact_response and resp.ok:
            logger.debug("← %s %s :: (respuesta omitida: contiene token)", resp.status_code, resp.reason)
        else:
            body = resp.text or ""
            if len(body) > _MAX_LOG_BODY:
                body = body[:_MAX_LOG_BODY] + "…(truncado)"
            logger.debug("← %s %s :: %s", resp.status_code, resp.reason, body)
        return resp

    # ------------------------------------------------------------------ auth
    def _ensure_token(self) -> str:
        # Renueva 60s antes de expirar para evitar carreras.
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        style = self.cfg.auth_style
        if style == "post":
            resp = self._token_request(use_basic=False)
        elif style == "basic":
            resp = self._token_request(use_basic=True)
        else:  # auto: primero body (como la colección Postman); si invalid_client, Basic.
            resp = self._token_request(use_basic=False)
            if resp.status_code in (400, 401):
                logger.debug("token con credenciales en body falló (%s); reintento con Basic", resp.status_code)
                resp = self._token_request(use_basic=True)

        if resp.status_code != 200:
            raise IGAMError(
                f"No se pudo obtener token OAuth2 ({resp.status_code}): {resp.text}\n"
                f"  'invalid_client' => client_id/client_secret incorrectos o app OAuth sin "
                f"grant_type=client_credentials. Verificá con 'python diagnose_invgate.py'."
            )

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise IGAMError(f"Respuesta de token sin access_token: {data}")

        self._access_token = token
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        logger.debug("token OAuth2 obtenido (expira en %ss)", data.get("expires_in"))
        return token

    def _token_request(self, use_basic: bool):
        """POST al endpoint de token. use_basic=True manda las credenciales por header
        Authorization: Basic (client_secret_basic); False las manda en el body.
        El body/credenciales NO se loguean; la respuesta exitosa se omite (trae token)."""
        body = {"grant_type": self.cfg.grant_type}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        auth = None
        if use_basic:
            auth = (self.cfg.client_id, self.cfg.client_secret)
        else:
            body["client_id"] = self.cfg.client_id
            body["client_secret"] = self.cfg.client_secret
        logger.debug("token request (estilo=%s)", "basic" if use_basic else "post")
        return self._send(
            "POST", self.cfg.token_url, data=body, headers=headers, auth=auth,
            log_request_body=False, redact_response=True,
        )

    def _headers(self, write: bool = False) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self._ensure_token()}"}
        if write:
            headers["Content-Type"] = JSON_API
            headers["Accept"] = JSON_API
        else:
            # Algunos endpoints (ej. asset-types) devuelven 406 si solo pedimos
            # vnd.api+json; ofrecemos también application/json para negociar.
            headers["Accept"] = "application/vnd.api+json, application/json"
        return headers

    # ------------------------------------------------------------------ reads
    def find_asset_by_serial(self, serial: str) -> Optional[Dict[str, Any]]:
        """Devuelve el primer asset cuyo serial coincide exacto, o None."""
        if not serial:
            return None
        resp = self._send(
            "GET", self.cfg.assets_lite_url, params={"serial": serial}, headers=self._headers(),
        )
        if resp.status_code != 200:
            raise IGAMError(f"Error buscando serial '{serial}' ({resp.status_code}): {resp.text}")
        data = _extract_list(resp.json())
        return data[0] if data else None

    def find_person_by_email(self, email: str) -> Optional[str]:
        """Resuelve un email a un Person.id existente (o None). Cachea resultados."""
        if not email:
            return None
        key = email.strip().lower()
        if key in self._person_cache:
            return self._person_cache[key]
        resp = self._send(
            "GET", f"{self.cfg.base_url}/public-api/people/",
            params={"email": email}, headers=self._headers(),
        )
        person_id = None
        if resp.status_code == 200:
            data = _extract_list(resp.json())
            if data:
                person_id = str(data[0].get("id"))
        self._person_cache[key] = person_id
        return person_id

    def list_custom_fields(self) -> List[Dict[str, Any]]:
        """Definiciones de custom fields de la instancia (para obtener sus IDs)."""
        return self._get_all(self.cfg.custom_fields_list_url)

    def set_custom_field(self, item: Dict[str, Any]):
        """POST /public-api/v2/custom-field-value-cis/ — UN custom field
        {custom_field_id, ci_id, ci_type, value}."""
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return self._send("POST", self.cfg.custom_field_value_url, json_body=item, headers=headers)

    def set_custom_fields(self, items: List[Dict[str, Any]]) -> tuple:
        """
        Carga los custom fields UNO POR UNO (el endpoint v2 es singular).
        Tolerante: un campo que falla no aborta el resto. Devuelve (ok, fail).
        """
        ok = fail = 0
        for item in items:
            try:
                resp = self.set_custom_field(item)
                if resp.status_code in (200, 201):
                    ok += 1
                else:
                    fail += 1
                    logger.warning(
                        "custom_field_id=%s falló (%s): %s",
                        item.get("custom_field_id"), resp.status_code, resp.text[:200],
                    )
            except Exception as exc:  # noqa: BLE001
                fail += 1
                logger.warning("custom_field_id=%s excepción: %s", item.get("custom_field_id"), exc)
        return ok, fail

    def list_locations(self) -> List[Dict[str, Any]]:
        return self._get_all(self.cfg.locations_url)

    def find_location_by_name(self, name: str) -> Optional[str]:
        """Resuelve un nombre de Location a su id (primer match). Cachea."""
        if not name:
            return None
        key = name.strip().lower()
        if key in self._location_cache:
            return self._location_cache[key]
        resp = self._send("GET", self.cfg.locations_url, params={"name": name.strip()}, headers=self._headers())
        location_id = None
        if resp.status_code == 200:
            data = _extract_list(resp.json())
            if data:
                location_id = str(data[0].get("id"))
        self._location_cache[key] = location_id
        return location_id

    def list_asset_types(self) -> List[Dict[str, Any]]:
        return self._get_all(f"{self.cfg.base_url}/public-api/asset-types/")

    def list_asset_statuses(self) -> List[Dict[str, Any]]:
        return self._get_all(f"{self.cfg.base_url}/public-api/asset-status/")

    def _get_all(self, url: str) -> List[Dict[str, Any]]:
        resp = self._send("GET", url, headers=self._headers())
        if resp.status_code != 200:
            raise IGAMError(f"GET {url} falló ({resp.status_code}): {resp.text}")
        return _extract_list(resp.json())

    # ------------------------------------------------------------------ writes
    def create_asset(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /public-api/assets-lite/  -> 201. Devuelve el asset creado."""
        resp = self._send(
            "POST", self.cfg.assets_lite_url, json_body=payload, headers=self._headers(write=True),
        )
        if resp.status_code not in (200, 201):
            raise IGAMError(f"Error creando asset ({resp.status_code}): {resp.text}")
        return resp.json().get("data", {})

    def update_asset(self, asset_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH /public-api/assets-lite/{id}/ -> 200. Actualización parcial."""
        url = f"{self.cfg.assets_lite_url}{asset_id}/"
        resp = self._send(
            "PATCH", url, json_body=payload, headers=self._headers(write=True),
        )
        if resp.status_code != 200:
            raise IGAMError(f"Error actualizando asset {asset_id} ({resp.status_code}): {resp.text}")
        return resp.json().get("data", {})
