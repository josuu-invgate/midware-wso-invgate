"""
Mapeo de un dispositivo de Workspace ONE (devices/search) a un asset de InvGate.

Salida principal: el payload JSON:API listo para POST/PATCH a /public-api/assets-lite/.

NOTA: los nombres de asset_type DEBEN existir en tu instancia
(ver GET /public-api/asset-types/). Ajustá TABLET_TYPE / PHONE_TYPE si difieren.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Importar config (que carga el .env) garantiza que los nombres de tipo salgan
# del .env y no de defaults — antes esto se leía antes de cargar el .env (bug).
from config import IGAM

# Marcas reconocibles a partir del campo Model (para Android).
_ANDROID_BRANDS = (
    "Samsung", "Galaxy", "Google", "Pixel", "Motorola", "Moto", "Xiaomi",
    "Redmi", "Huawei", "OnePlus", "Sony", "LG", "Nokia", "Oppo", "Realme",
    "Lenovo", "Zebra", "Honeywell", "Datalogic",
)


# --------------------------------------------------------------------------- ids
def device_id(device: Dict[str, Any]) -> Optional[str]:
    """El Id viene como objeto {Value: <int>}; devolvemos el entero como string."""
    raw = device.get("Id")
    if isinstance(raw, dict):
        val = raw.get("Value")
        return str(val) if val is not None else None
    return str(raw) if raw is not None else None


def dedup_serial(device: Dict[str, Any]) -> Optional[str]:
    """
    Clave única / de deduplicación.
    SerialNumber suele estar; si falta (iOS viejos, algunos Android) caemos a
    Imei y luego Uuid para que SIEMPRE haya un serial estable y único.
    """
    return device.get("SerialNumber") or device.get("Imei") or device.get("Uuid") or None


def _obj_name(device: Dict[str, Any], key: str) -> Optional[str]:
    """Nombre dentro de objetos anidados WS1 tipo {Id:{Value}, Name, Uuid}."""
    obj = device.get(key)
    if isinstance(obj, dict):
        return obj.get("Name") or None
    return None


def _obj_id(device: Dict[str, Any], key: str) -> Optional[Any]:
    """Id.Value dentro de objetos anidados WS1 tipo {Id:{Value}, Name}."""
    obj = device.get(key)
    if isinstance(obj, dict):
        inner = obj.get("Id")
        if isinstance(inner, dict):
            return inner.get("Value")
    return None


# ------------------------------------------------------------- normalizadores
_NON_HEX = re.compile(r"[^0-9A-Fa-f]")


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """Normaliza la MAC al formato AA:BB:CC:DD:EE:FF (mayúsculas, separada por ':')."""
    if not mac:
        return None
    hexd = _NON_HEX.sub("", str(mac)).upper()
    if len(hexd) != 12:
        return None  # no es una MAC de 48 bits válida
    return ":".join(hexd[i:i + 2] for i in range(0, 12, 2))


def normalize_imei(imei: Optional[str]) -> Optional[str]:
    """IMEI = 14 a 16 dígitos (regex ^[0-9]{14,16}$). Limpia espacios/guiones."""
    if not imei:
        return None
    digits = re.sub(r"\D", "", str(imei))
    return digits if re.fullmatch(r"[0-9]{14,16}", digits) else None


def _gb_to_mb(value: Any) -> Optional[int]:
    """Convierte capacidad en GB (como la da WS1) a MB (como la espera InvGate)."""
    try:
        gb = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(gb * 1024)) if gb > 0 else None


def _to_mb(value: Any) -> Optional[int]:
    """Valor que YA viene en MB -> int (RAM/almacenamiento ya en MB)."""
    try:
        mb = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(mb)) if mb > 0 else None


def _memory_mb(device: Dict[str, Any], primary_key: str, info_key: str) -> Optional[int]:
    """
    Memoria en MB. WS1 da el total en dos formas:
      - <primary_key> ya en MB (ej. TotalPhysicalMemory=1837)
      - <info_key> = {MemorySize, MemoryUnit} (ej. {1.79, "GB"})
    Preferimos el valor directo; si no, convertimos desde el objeto Info.
    """
    direct = _to_mb(device.get(primary_key))
    if direct:
        return direct
    info = device.get(info_key)
    if isinstance(info, dict):
        try:
            size = float(info.get("MemorySize"))
        except (TypeError, ValueError):
            return None
        unit = (info.get("MemoryUnit") or "MB").upper()
        factor = {"TB": 1024 * 1024, "GB": 1024, "MB": 1}.get(unit, 1)
        return int(round(size * factor)) if size > 0 else None
    return None


def _battery_level(value: Any) -> Optional[str]:
    """Normaliza el nivel de batería al formato de InvGate ('91%')."""
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("%"):
        return s
    try:
        n = float(s)
        n = n * 100 if n <= 1 else n  # 0.91 -> 91
        return f"{int(round(n))}%"
    except ValueError:
        return s


def _cellular(device: Dict[str, Any], *keys: str) -> Optional[str]:
    """Busca un valor dentro de DeviceCellularNetworkInfo[] (datos de la SIM/operadora)."""
    info = device.get("DeviceCellularNetworkInfo")
    if isinstance(info, list):
        for entry in info:
            if isinstance(entry, dict):
                for key in keys:
                    if entry.get(key):
                        return entry[key]
    return None


# ------------------------------------------------------------------- extractores
def extract_ip(device: Dict[str, Any]) -> Optional[str]:
    """IP principal: top-level o el primer DeviceNetworkInfo con IPAddress."""
    if device.get("IPAddress"):
        return device["IPAddress"]
    net = device.get("DeviceNetworkInfo")
    if isinstance(net, list):
        for entry in net:
            if isinstance(entry, dict) and entry.get("IPAddress"):
                return entry["IPAddress"]
    return None


def guess_asset_type(device: Dict[str, Any]) -> str:
    model = (device.get("Model") or "").lower()
    if any(k in model for k in ("ipad", "tab", "tablet", "surface")):
        return IGAM.tablet_type
    return IGAM.phone_type


def guess_manufacturer(device: Dict[str, Any]) -> Optional[str]:
    platform = (device.get("Platform") or "").strip()
    # OEMInfo (ej. "Honeywell EDA51") y Model son las mejores pistas de marca.
    haystack = f"{device.get('OEMInfo') or ''} {device.get('Model') or ''}"
    if platform == "Apple":
        return "Apple"
    for brand in _ANDROID_BRANDS:
        if brand.lower() in haystack.lower():
            # Normalizamos algunos alias a la marca real.
            if brand in ("Galaxy",):
                return "Samsung"
            if brand in ("Pixel",):
                return "Google"
            if brand in ("Moto",):
                return "Motorola"
            if brand in ("Redmi",):
                return "Xiaomi"
            return brand
    # Último recurso: primera palabra de OEMInfo (suele ser el fabricante).
    oem = (device.get("OEMInfo") or "").strip()
    if oem:
        return oem.split()[0]
    if platform == "Android":
        return "Android"
    return platform or None


# --------------------------------------------------------------------- atributos
def device_to_attributes(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bloque 'attributes' para POST /public-api/assets-lite/.

    IMPORTANTE: assets-lite SOLO persiste estos atributos. Los técnicos
    (ram, mac, imei, storage, etc.) son de solo-lectura por API (los llena el
    agente) y se IGNORAN si se mandan acá -> van por CUSTOM FIELDS (ver
    custom_values / custom_fields.py).
    """
    name = (
        device.get("DeviceFriendlyName")
        or device.get("DeviceReportedName")
        or device.get("Model")
        or f"WS1 Device {device_id(device)}"
    )
    serial = dedup_serial(device)
    inventory_id = device.get("AssetNumber") or device.get("Udid") or device.get("Uuid") or None

    attrs: Dict[str, Any] = {
        "name": name,
        "asset_type": guess_asset_type(device),
    }
    if serial:
        attrs["serial"] = serial
    if inventory_id:
        attrs["inventory_id"] = inventory_id
    if device.get("Model"):
        attrs["model"] = device["Model"]
    manufacturer = guess_manufacturer(device)
    if manufacturer:
        attrs["manufacturer"] = manufacturer
    ip = extract_ip(device)
    if ip:
        attrs["default_ip"] = ip
    return attrs


def custom_values(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    TODOS los valores mapeables a CUSTOM FIELDS, por clave lógica.
    custom_fields.json referencia estas claves. Incluye los técnicos (que
    assets-lite ignora) + los identificadores de extract_identifiers().
    """
    vals: Dict[str, Any] = dict(extract_identifiers(device))
    # Técnicos (los custom fields que creó el usuario: RAM, Storage, MAC, IPv4, etc.)
    vals["ram"] = _memory_mb(device, "TotalPhysicalMemory", "TotalPhysicalMemoryInfo")
    vals["storage_total"] = _gb_to_mb(device.get("DeviceCapacity"))           # iOS
    vals["storage_available"] = _gb_to_mb(device.get("AvailableDeviceCapacity"))
    vals["mac"] = normalize_mac(device.get("MacAddress"))
    vals["ipv4"] = extract_ip(device)                                        # no viene en search
    vals["imei"] = normalize_imei(device.get("Imei"))
    vals["phone_number"] = device.get("PhoneNumber") or _cellular(device, "PhoneNumber", "MSISDN")
    vals["carrier"] = device.get("CarrierName") or _cellular(
        device, "CarrierName", "OperatorName", "CurrentCarrierNetwork"
    )
    vals["screen_size"] = None   # no viene en devices/search
    vals["processor"] = None     # no viene en devices/search
    vals["battery_level"] = _battery_level(device.get("BatteryLevel"))
    return vals


def extract_identifiers(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    TODOS los datos extra de WS1 que NO tienen atributo nativo en InvGate Phone.
    Sirven para: (a) custom fields, (b) resolver owner/location, (c) inspección.
    Las claves de acá son las que se referencian en custom_fields.json.
    """
    ownership_code = device.get("Ownership") or None
    ownership_label = {
        "C": "Corporate", "E": "Employee", "S": "Shared", "Undefined": "Undefined",
    }.get(ownership_code, ownership_code)
    return {
        # identificadores
        "udid": device.get("Udid") or None,
        "uuid": device.get("Uuid") or None,
        "ws1_device_id": device_id(device),
        "imei": device.get("Imei") or None,
        "mac": device.get("MacAddress") or None,
        # sistema operativo
        "os": device.get("OperatingSystem") or None,
        "os_build": device.get("OSBuildVersion") or None,
        "platform": device.get("Platform") or _obj_name(device, "PlatformId"),
        # estado / seguridad
        "enrollment_status": device.get("EnrollmentStatus") or None,
        "compliance_status": device.get("ComplianceStatus") or None,
        "compromised": device.get("CompromisedStatus"),
        "is_supervised": device.get("IsSupervised"),
        "last_seen": device.get("LastSeen") or None,
        "last_enrolled_on": device.get("LastEnrolledOn") or None,
        "security_patch_date": device.get("SecurityPatchDate") or None,
        # red / propiedad / gestión
        "wifi_ssid": device.get("WifiSsid") or None,
        "ownership": ownership_code,
        "ownership_label": ownership_label,
        "managed_by": device.get("ManagedBy"),
        "device_reported_name": device.get("DeviceReportedName") or None,
        # organización / usuario (para resolver location/owner)
        "location_group_name": (device.get("LocationGroupName") or "").strip() or None,
        "location_group_id": _obj_id(device, "LocationGroupId"),
        "user_name": device.get("UserName") or None,
        "user_email": device.get("UserEmailAddress") or None,
    }


# ------------------------------------------------------------------- relationships
def _relationships(
    status_id: Optional[str],
    location_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    rels: Dict[str, Any] = {}
    if status_id:
        rels["status"] = {"data": {"type": "AssetStatus", "id": str(status_id)}}
    if location_id:
        rels["location"] = {"data": {"type": "Location", "id": str(location_id)}}
    if owner_id:
        rels["owner"] = {"data": {"type": "Person", "id": str(owner_id)}}
    return rels


def build_create_payload(
    attributes: Dict[str, Any],
    status_id: Optional[str] = None,
    location_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Sobre JSON:API para POST /public-api/assets-lite/ (sin id, lo asigna el server)."""
    data: Dict[str, Any] = {"type": "Asset", "attributes": attributes}
    rels = _relationships(status_id, location_id, owner_id)
    if rels:
        data["relationships"] = rels
    return {"data": data}


def build_update_payload(
    asset_id: str,
    attributes: Dict[str, Any],
    status_id: Optional[str] = None,
    location_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Sobre JSON:API para PATCH /public-api/assets-lite/{id}/ (incluye id)."""
    data: Dict[str, Any] = {"type": "Asset", "id": str(asset_id), "attributes": attributes}
    rels = _relationships(status_id, location_id, owner_id)
    if rels:
        data["relationships"] = rels
    return {"data": data}
