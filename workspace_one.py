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

    def get_smartgroup_raw(self, smart_group_id: Any) -> Dict[str, Any]:
        """Respuesta cruda de GET /api/mdm/smartgroups/{id} (para inspección). {} si falla."""
        url = f"{self.cfg.base_url}/api/mdm/smartgroups/{smart_group_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("smartgroup %s excepción: %s", smart_group_id, exc)
            return {}
        if resp.status_code != 200:
            logger.warning("smartgroup %s -> %s: %s", smart_group_id, resp.status_code, resp.text[:200])
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_device_id(item: Any) -> Optional[str]:
        """Saca un device id de un item que puede ser int, str o dict ({Id}/{Value}/{DeviceId})."""
        if isinstance(item, (int, str)):
            return str(item)
        if isinstance(item, dict):
            did = item.get("Id", item.get("DeviceId", item.get("Value")))
            if isinstance(did, dict):
                did = did.get("Value")
            return str(did) if did is not None else None
        return None

    def get_smartgroup_device_ids(self, smart_group_id: Any) -> set:
        """
        IDs de los devices que pertenecen a un Smart Group.
        El shape varía por tenant: la lista puede estar en 'Devices'/'DeviceAdditions'
        (lista de dicts/ids), o esas claves pueden ser un CONTEO (int). Buscamos en
        las claves candidatas y, si no, en cualquier clave que sea lista de devices.
        """
        data = self.get_smartgroup_raw(smart_group_id)
        if not data:
            return set()

        logger.debug("smartgroup %s keys: %s",
                     smart_group_id, {k: type(v).__name__ for k, v in data.items()})

        ids = set()
        # 1) Claves conocidas que sean LISTAS.
        for key in ("Devices", "DeviceAdditions", "DeviceIds", "device_additions"):
            val = data.get(key)
            if isinstance(val, list):
                for item in val:
                    did = self._extract_device_id(item)
                    if did:
                        ids.add(did)
            elif isinstance(val, dict) and isinstance(val.get("Devices"), list):
                for item in val["Devices"]:
                    did = self._extract_device_id(item)
                    if did:
                        ids.add(did)

        # 2) Si no encontramos nada, recorremos cualquier clave que sea lista de dicts con Id.
        if not ids:
            for key, val in data.items():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    for item in val:
                        did = self._extract_device_id(item)
                        if did:
                            ids.add(did)
            if ids:
                logger.debug("smartgroup %s: ids hallados por barrido genérico", smart_group_id)

        logger.debug("smartgroup %s: %d device ids", smart_group_id, len(ids))
        return ids

    def iter_mobile_devices(self) -> Iterator[Dict[str, Any]]:
        """
        Itera los dispositivos móviles enrolados.

        Hace UN solo barrido SIN el parámetro 'platform' en la query (evita el
        204 que da 'platform=Apple' y no recorre plataformas no-móviles como
        AppleOsX/WindowsPc) y filtra móvil EN EL CLIENTE.

        Si WS1_SMART_GROUP_ID está seteado, además filtra a los devices que
        pertenecen a ese smart group.
        """
        wanted = [p for p in self.cfg.platforms if p in MOBILE_PLATFORMS] or list(MOBILE_PLATFORMS)
        logger.debug("Filtrando a plataformas móviles: %s", wanted)

        sg_ids = None
        if self.cfg.smart_group_id:
            sg_ids = self.get_smartgroup_device_ids(self.cfg.smart_group_id)
            logger.debug("Smart group %s: %d devices a importar", self.cfg.smart_group_id, len(sg_ids))
            if not sg_ids:
                logger.warning("Smart group %s sin devices (o no se pudo leer): no se importará nada.",
                               self.cfg.smart_group_id)

        for dev in self._iter_platform(None):  # None => sin parámetro platform
            if sg_ids is not None:
                raw = dev.get("Id")
                did = raw.get("Value") if isinstance(raw, dict) else raw
                if str(did) not in sg_ids:
                    continue
            if (dev.get("Platform") or "") not in wanted:
                continue
            if self.cfg.only_enrolled and not self._is_enrolled(dev):
                continue
            yield dev

    def fetch_mobile_devices(self) -> List[Dict[str, Any]]:
        return list(self.iter_mobile_devices())

    # ------------------------------------------------------------------ detalle
    def get_device_detail(self, device_id: Any, version: int = 2) -> Dict[str, Any]:
        """
        Detalle de un device: GET /api/mdm/devices/{id}.
        Pedimos version=2 (la v1 no trae storage); este modelo es el que puede
        exponer el almacenamiento. Tolerante: devuelve {} si falla.
        """
        if not device_id:
            return {}
        url = f"{self.cfg.base_url}/api/mdm/devices/{device_id}"
        headers = {"Accept": f"application/json;version={version}"} if version else None
        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug("device detail %s excepción: %s", device_id, exc)
            return {}
        if resp.status_code != 200:
            logger.debug("device detail %s (v%s) -> %s", device_id, version, resp.status_code)
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
        Agrega los campos de almacenamiento al device, tomándolos del detalle por id.
        Como el nombre del campo varía por tenant/versión, mergeamos:
          - los nombres conocidos (TotalStorageBytes/AvailableStorageBytes/DataEncrypted)
          - cualquier clave que contenga 'Storage' o 'Capacity' (descubrimiento).
        """
        raw = device.get("Id")
        device_id = raw.get("Value") if isinstance(raw, dict) else raw
        detail = self.get_device_detail(device_id)
        if not isinstance(detail, dict):
            return device
        for key, value in detail.items():
            kl = key.lower()
            if key in ("TotalStorageBytes", "AvailableStorageBytes", "DataEncrypted") \
                    or "storage" in kl or "capacity" in kl:
                if value is not None and key not in device:
                    device[key] = value
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
