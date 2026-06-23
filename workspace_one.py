"""
Cliente de Workspace ONE UEM (API MDM) para traer dispositivos móviles.

Endpoint STANDARD: GET /api/mdm/devices/search

Autenticación (igual que en el doc mamv2.json):
  - Authorization: Basic base64(usuario:password)   (usuario admin con permiso API/Devices)
  - aw-tenant-code: <API Key del tenant>
  - Accept: application/json   (v1 es la más completa para devices/search)

Notas de contrato (verificadas):
  - La paginación es BASE 0 (la primera página es page=0).
  - 'pagesize' por defecto es 10; lo subimos (500) para inventarios.
  - El sobre de respuesta es {"Devices": [...], "Total": N, "Page": p, "PageSize": s}.
  - 'platform' acepta UN valor por llamada -> se itera por plataforma.
  - El Id viene como objeto: leer el entero desde Id.Value.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from config import WS1Config

# Plataformas consideradas "móviles" para este import.
MOBILE_PLATFORMS = ("Apple", "Android")

logger = logging.getLogger("workspace_one")


class WS1Error(RuntimeError):
    """Error devuelto por la API de Workspace ONE UEM."""


class WorkspaceOneClient:
    def __init__(self, cfg: WS1Config, timeout: int = 60, verify_tls: bool = True):
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls

        token = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
        # Para devices/search el scope se hace con el parámetro lgid (no con el header
        # aw-groupid), así que NO seteamos aw-groupid acá para no disparar 500.
        self.session.headers.update(
            {
                "Authorization": f"Basic {token}",
                "aw-tenant-code": cfg.tenant_code,
                "Accept": cfg.accept_header,
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------ search
    def _search_page(self, platform: Optional[str], page: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "pagesize": self.cfg.page_size}
        if platform:
            params["platform"] = platform
        if self.cfg.organization_group_id:
            params["lgid"] = self.cfg.organization_group_id

        resp = self._get_with_retry(params)
        if resp.status_code != 200:
            raise WS1Error(
                f"devices/search falló (platform={platform or 'todas'}, page={page}, "
                f"status={resp.status_code}): {resp.text[:500]}\n"
                f"  Sugerencias: (1) seteá WS1_ORGANIZATION_GROUP_ID (lgid) para acotar el "
                f"tenant; (2) bajá WS1_PAGE_SIZE (ej. 50); (3) probá WS1_PLATFORMS= vacío "
                f"(el filtro platform a veces dispara 500). Corré 'python diagnose_ws1.py'."
            )
        return resp.json()

    def _get_with_retry(self, params: Dict[str, Any], retries: int = 2):
        """GET con reintentos ante 429 / 5xx transitorios (backoff simple)."""
        last = None
        for attempt in range(retries + 1):
            logger.debug("→ GET %s params=%s (intento %d)", self.cfg.devices_search_url, params, attempt + 1)
            last = self.session.get(self.cfg.devices_search_url, params=params, timeout=self.timeout)
            count = "-"
            try:
                count = len((last.json() or {}).get("Devices") or []) if last.status_code == 200 else "-"
            except Exception:
                pass
            logger.debug("← %s %s (devices en página=%s)", last.status_code, last.reason, count)
            if last.status_code not in (429, 500, 502, 503, 504):
                return last
            if attempt < retries:
                time.sleep(2 * (attempt + 1))  # 2s, 4s
        return last

    def _iter_platform(self, platform: Optional[str]) -> Iterator[Dict[str, Any]]:
        """Itera TODAS las páginas (base 0) de una plataforma."""
        page = 0
        seen = 0
        while True:
            data = self._search_page(platform, page)
            devices = data.get("Devices") or []
            for dev in devices:
                yield dev
            seen += len(devices)

            # Página vacía => no hay más nada.
            if not devices:
                break
            # Si el tenant informa Total (>0), cortamos al alcanzarlo.
            total = int(data.get("Total", 0) or 0)
            if total > 0 and seen >= total:
                break
            # Si no informa Total, una página más chica que pagesize es la última.
            if total <= 0 and len(devices) < self.cfg.page_size:
                break
            page += 1

    def iter_mobile_devices(self) -> Iterator[Dict[str, Any]]:
        """
        Itera los dispositivos móviles enrolados.

        Hace UN solo barrido SIN el parámetro 'platform' en la query (evita el
        204 que da 'platform=Apple' y no recorre plataformas no-móviles como
        AppleOsX/WindowsPc) y filtra móvil EN EL CLIENTE.

        Plataformas a conservar: las de WS1_PLATFORMS que sean móviles; si no hay
        ninguna móvil configurada, se usan todas las móviles (Apple, Android).
        """
        wanted = [p for p in self.cfg.platforms if p in MOBILE_PLATFORMS] or list(MOBILE_PLATFORMS)
        logger.debug("Filtrando a plataformas móviles: %s", wanted)
        for dev in self._iter_platform(None):  # None => sin parámetro platform
            if (dev.get("Platform") or "") not in wanted:
                continue
            if self.cfg.only_enrolled and not self._is_enrolled(dev):
                continue
            yield dev

    def fetch_mobile_devices(self) -> List[Dict[str, Any]]:
        return list(self.iter_mobile_devices())

    # ------------------------------------------------------------------ detalle
    def get_device_detail(self, device_id: Any) -> Dict[str, Any]:
        """
        Detalle de un device: GET /api/mdm/devices/{id}.
        A diferencia de search, este modelo expone TotalStorageBytes /
        AvailableStorageBytes (bytes) — el ÚNICO modo de obtener storage en Android.
        Tolerante: devuelve {} si falla (no aborta el lote).
        """
        if not device_id:
            return {}
        url = f"{self.cfg.base_url}/api/mdm/devices/{device_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug("device detail %s excepción: %s", device_id, exc)
            return {}
        if resp.status_code != 200:
            logger.debug("device detail %s -> %s", device_id, resp.status_code)
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    def get_device_by_serial(self, serial: str) -> Dict[str, Any]:
        """
        Trae UN device por serial: GET /api/mdm/devices?searchby=Serialnumber&id=<serial>.
        Devuelve el modelo de detalle (incluye TotalStorageBytes etc.) sin depender de
        los filtros de móvil/enrolado. {} si no se encuentra.
        """
        if not serial:
            return {}
        url = f"{self.cfg.base_url}/api/mdm/devices"
        try:
            resp = self.session.get(
                url, params={"searchby": "Serialnumber", "id": serial}, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_device_by_serial %s excepción: %s", serial, exc)
            return {}
        if resp.status_code != 200:
            logger.debug("get_device_by_serial %s -> %s", serial, resp.status_code)
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        # Normalizamos: puede venir como dict del device, lista, o {Devices:[...]}.
        if isinstance(data, dict):
            if isinstance(data.get("Devices"), list):
                return data["Devices"][0] if data["Devices"] else {}
            return data
        if isinstance(data, list):
            return data[0] if data else {}
        return {}

    def enrich_storage(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """
        Agrega TotalStorageBytes/AvailableStorageBytes (y DataEncrypted) al device,
        tomándolos del detalle por id. Funciona para Android e iOS.
        """
        raw = device.get("Id")
        device_id = raw.get("Value") if isinstance(raw, dict) else raw
        detail = self.get_device_detail(device_id)
        for key in ("TotalStorageBytes", "AvailableStorageBytes", "DataEncrypted"):
            if detail.get(key) is not None:
                device[key] = detail[key]
        return device

    # ------------------------------------------------------------------ filtros
    @staticmethod
    def _is_mobile(device: Dict[str, Any]) -> bool:
        platform = (device.get("Platform") or "").strip()
        # Si no vino Platform, lo dejamos pasar (ya se filtró por query si correspondía).
        return platform in MOBILE_PLATFORMS if platform else True

    @staticmethod
    def _is_enrolled(device: Dict[str, Any]) -> bool:
        status = (device.get("EnrollmentStatus") or "").strip().lower()
        return status == "enrolled" if status else True
