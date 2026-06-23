"""
Resolución de Location de InvGate desde el grupo organizacional de WS1.

El LocationGroupName de WS1 (ej "LOJAS SÃO PAULO") NO coincide con la Location
de InvGate (ej "São Paulo"), así que se usa una tabla de traducción explícita:

location_map.json (en esta carpeta), con cualquiera de estas formas por entrada:
    { "<LocationGroupId numérico>": "<location_id de InvGate>" }   # directo por id
    { "<LocationGroupName>":       "<location_id de InvGate>" }    # directo por nombre
    { "<LocationGroupName>":       "São Paulo" }                   # nombre InvGate a resolver

Si el valor es numérico -> se usa como location_id directo.
Si es texto -> se resuelve por nombre contra GET /public-api/locations/.
Obtené los IDs/nombres con: python diagnose_invgate.py (sección LOCATIONS).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import mapping

_FILE = os.getenv("IGAM_LOCATION_MAP_FILE", "location_map.json")


def load_map() -> Dict[str, str]:
    path = Path(__file__).resolve().parent / _FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items()
            if v and not str(k).startswith("_")}


def resolve(device: Dict[str, Any], igam, default_id: Optional[str]) -> Optional[str]:
    """Devuelve el location_id de InvGate para el device, o default_id."""
    field_map = load_map()
    if not field_map:
        return default_id

    name = (device.get("LocationGroupName") or "").strip()
    gid = mapping._obj_id(device, "LocationGroupId")
    target = field_map.get(str(gid)) or field_map.get(name)
    if not target:
        return default_id
    if str(target).isdigit():
        return str(target)          # ya es un location_id
    return igam.find_location_by_name(target) or default_id
