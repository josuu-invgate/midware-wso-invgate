"""
Diagnóstico de la API devices/search de Workspace ONE.

Prueba combinaciones de parámetros (de menos a más) para aislar de dónde sale
el 500 "Internal Server Error" y al final te dice qué poner en el .env.

Uso:
    python diagnose_ws1.py
"""
from __future__ import annotations

import base64
import sys
from typing import Any, Dict, Optional

import requests

from config import SYNC, WS1

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def mask(value: Optional[str]) -> str:
    if not value:
        return "(vacío)"
    value = str(value)
    return f"{value[:3]}…{value[-3:]}" if len(value) > 8 else "***"


BASE = WS1.devices_search_url
OG = WS1.organization_group_id
TOKEN = base64.b64encode(f"{WS1.username}:{WS1.password}".encode()).decode()
CORE = {
    "Authorization": f"Basic {TOKEN}",
    "aw-tenant-code": WS1.tenant_code,
    "Content-Type": "application/json",
}

session = requests.Session()
session.verify = SYNC.verify_tls

results: Dict[str, Optional[int]] = {}


def attempt(
    key: str,
    desc: str,
    params: Dict[str, Any],
    accept: str = "application/json",
    groupid: Optional[str] = None,
) -> Optional[int]:
    headers = dict(CORE)
    headers["Accept"] = accept
    if groupid:
        headers["aw-groupid"] = str(groupid)
    try:
        r = session.get(BASE, params=params, headers=headers, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"  [EXC ] {desc}: {exc}")
        results[key] = None
        return None

    ok = r.status_code == 200
    extra = ""
    if ok:
        try:
            j = r.json()
            extra = f" (Total={j.get('Total')}, en página={len(j.get('Devices') or [])})"
        except Exception:
            extra = " (200 pero respuesta no-JSON)"
    if r.status_code == 204:
        flag, extra = "VACÍO", " (204 No Content = 0 dispositivos para ese filtro)"
    elif ok:
        flag = "OK  "
    else:
        flag = "FAIL"
    body = "" if r.status_code in (200, 204) else " :: " + r.text[:160].replace("\n", " ")
    print(f"  [{flag}] {desc} -> {r.status_code}{extra}{body}")
    results[key] = r.status_code
    return r.status_code


def census() -> None:
    """Cuenta los valores de Platform en la 1ª página para ver qué hay realmente."""
    from collections import Counter

    headers = dict(CORE)
    headers["Accept"] = "application/json"
    params: Dict[str, Any] = {"page": 0, "pagesize": 500}
    if OG:
        params["lgid"] = OG
    try:
        r = session.get(BASE, params=params, headers=headers, timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  (no se pudo censar plataformas: {exc})")
        return
    if r.status_code != 200:
        print(f"  (no se pudo censar plataformas: HTTP {r.status_code})")
        return
    devices = r.json().get("Devices") or []
    plat = Counter((d.get("Platform") or "(sin Platform)") for d in devices)
    enrolled = Counter((d.get("EnrollmentStatus") or "(sin estado)") for d in devices)
    print(f"  Plataformas en la 1ª página ({len(devices)} devices):")
    for k, v in plat.most_common():
        print(f"      Platform = {k}: {v}")
    print("  EnrollmentStatus:")
    for k, v in enrolled.most_common():
        print(f"      {k}: {v}")


def main() -> None:
    print("=" * 72)
    print("  Diagnóstico Workspace ONE  -  devices/search")
    print(f"  URL          : {BASE}")
    print(f"  Usuario      : {WS1.username}")
    print(f"  aw-tenant-code: {mask(WS1.tenant_code)}")
    print(f"  OG (lgid)    : {OG or '(NO seteado en .env)'}")
    print(f"  Accept base  : {WS1.accept_header}")
    print("=" * 72)
    print("Pruebas (de menos a más parámetros):")

    lgid = {"lgid": OG} if OG else {}

    attempt("minimal", "1) pagesize=1, sin filtros, sin scope", {"page": 0, "pagesize": 1})
    if OG:
        attempt("lgid", "2) + lgid (param)", {"page": 0, "pagesize": 1, **lgid})
        attempt("groupid", "3) + aw-groupid (header)", {"page": 0, "pagesize": 1}, groupid=OG)
    else:
        print("  [SKIP] 2-3) No hay WS1_ORGANIZATION_GROUP_ID en .env (probá con 14407).")
    attempt("platform", "4) + platform=Apple (con scope si hay)", {"page": 0, "pagesize": 1, "platform": "Apple", **lgid})
    attempt("v2", "5) version=2 (con scope si hay)", {"page": 0, "pagesize": 1, **lgid}, accept="application/json;version=2")
    attempt("big", "6) pagesize=500 (con scope si hay)", {"page": 0, "pagesize": 500, **lgid})

    print("-" * 72)
    print("CENSO DE PLATAFORMAS:")
    census()

    print("-" * 72)
    print("RECOMENDACIÓN:")
    _recommend()
    print("=" * 72)


def _recommend() -> None:
    minimal = results.get("minimal")
    platform = results.get("platform")
    big = results.get("big")
    v2 = results.get("v2")
    groupid = results.get("groupid")

    # Las credenciales sólo están mal si falla la prueba 1 (que no usa headers de grupo).
    if minimal in (401, 403):
        print("  • La prueba 1 (sin scope) falla con auth -> revisá WS1_USERNAME / WS1_PASSWORD / WS1_TENANT_CODE.")
        return

    if minimal == 200:
        print("  • Las credenciales están OK (prueba 1 = 200) y la búsqueda funciona.")

    if groupid in (401, 403):
        print("  • El header aw-groupid=14407 da 401 (errorCode 1019). Es ESPERADO y no afecta:")
        print("      el sync NO usa ese header; el scope va por el parámetro lgid. No toques nada por esto.")

    if platform == 204:
        print("  • platform=Apple devolvió 0 dispositivos (204): tu flota no tiene iOS (o el valor no matchea).")
        print("      -> Dejá  WS1_PLATFORMS=  (vacío) en el .env y se filtran móviles en código.")
    elif platform and platform not in (200, 204):
        print("      -> El parámetro 'platform' rompe (HTTP %s). Usá  WS1_PLATFORMS=  (vacío)." % platform)

    if big and big != 200:
        print("      -> pagesize grande falla -> bajá  WS1_PAGE_SIZE=100  (o 50).")
    elif big == 200:
        print("  • pagesize=500 funciona bien.")

    if v2 == 200:
        print("  • Recordá: dejá  WS1_API_VERSION=  (vacío) o =2, pero NO 'V1' (mal formado).")

    print("  Mirá el CENSO de arriba: si ves Platform=Android, esos son los que se van a importar.")


if __name__ == "__main__":
    main()
