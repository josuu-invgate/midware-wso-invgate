"""
Custom fields: carga el mapa custom_fields.json y arma el payload del endpoint
v2 /public-api/custom-field-value-cis/multiple para los datos de WS1 que NO
tienen atributo nativo en InvGate (UDID, OS, enrollment, compliance, WiFi, etc.).

custom_fields.json (en esta carpeta) tiene la forma:
    { "<custom_field_id>": "<clave_de_extract_identifiers>" }
Ej:
    {
      "101": "udid",
      "102": "os",
      "103": "enrollment_status",
      "104": "compliance_status",
      "105": "wifi_ssid",
      "106": "ownership_label"
    }

Las claves de la derecha son las que devuelve mapping.custom_values()
(ram, storage_total, mac, ipv4, imei, phone_number, carrier, udid, os, etc.).
Los IDs (izquierda) salen de:  python diagnose_invgate.py  (lista los custom fields).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import mapping

# ci_type del endpoint v2 va en minúscula ('phone'), aunque asset_type sea 'Phone'.
CI_TYPE = os.getenv("IGAM_CI_TYPE", "phone").strip()
_FILE = os.getenv("IGAM_CUSTOM_FIELDS_FILE", "custom_fields.json")


def load_map() -> Dict[str, str]:
    """Lee custom_fields.json -> { '<custom_field_id>': '<clave_identifier>' }. Vacío si no existe."""
    path = Path(__file__).resolve().parent / _FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): str(v) for k, v in data.items() if v and not str(k).startswith("_")}


def build_items(device: Dict[str, Any], ci_id: Any, field_map: Dict[str, str]) -> List[Dict[str, Any]]:
    """Arma la lista de items para set_custom_fields(). Omite valores vacíos."""
    if not field_map or not ci_id:
        return []
    ids = mapping.custom_values(device)
    items: List[Dict[str, Any]] = []
    for cf_id, key in field_map.items():
        value = ids.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        items.append({
            "custom_field_id": int(cf_id) if str(cf_id).isdigit() else cf_id,
            "ci_id": int(ci_id) if str(ci_id).isdigit() else ci_id,
            "ci_type": CI_TYPE,
            "value": str(value),
        })
    return items
