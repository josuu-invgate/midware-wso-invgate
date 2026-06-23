"""
Diagnóstico de la autenticación OAuth2 de InvGate Asset Management.

Verifica que client_id/client_secret estén bien y prueba los dos estilos de
auth (credenciales en el body vs. header Basic) para aislar el 'invalid_client'.

Uso:
    python diagnose_invgate.py
"""
from __future__ import annotations

import sys
from typing import Optional

import requests

from config import IGAM, SYNC

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def mask(value: Optional[str]) -> str:
    if not value:
        return "(VACÍO!)"
    value = str(value)
    if value == "CHANGE_ME":
        return "CHANGE_ME (¡no completado!)"
    return f"{value[:4]}…{value[-3:]}  (len={len(value)})" if len(value) > 9 else f"*** (len={len(value)})"


session = requests.Session()
session.verify = SYNC.verify_tls


def token_request(use_basic: bool):
    body = {"grant_type": IGAM.grant_type}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = None
    if use_basic:
        auth = (IGAM.client_id, IGAM.client_secret)
    else:
        body["client_id"] = IGAM.client_id
        body["client_secret"] = IGAM.client_secret
    try:
        return session.post(IGAM.token_url, data=body, headers=headers, auth=auth, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"  [EXC] excepción de red: {exc}")
        return None


def show(label: str, resp) -> bool:
    if resp is None:
        return False
    ok = resp.status_code == 200 and "access_token" in resp.text
    if ok:
        try:
            j = resp.json()
            print(f"  [OK  ] {label} -> 200  (token ok, expires_in={j.get('expires_in')}, scope={j.get('scope')})")
        except Exception:
            print(f"  [OK  ] {label} -> 200")
    else:
        print(f"  [FAIL] {label} -> {resp.status_code} :: {resp.text[:200]}")
    return ok


def _list_asset_types(use_basic: bool) -> None:
    """Lista los asset_type y status disponibles para que veas los nombres EXACTOS."""
    # Obtener un token (el estilo que haya funcionado).
    body = {"grant_type": IGAM.grant_type}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = (IGAM.client_id, IGAM.client_secret) if use_basic else None
    if not use_basic:
        body["client_id"] = IGAM.client_id
        body["client_secret"] = IGAM.client_secret
    try:
        tok = session.post(IGAM.token_url, data=body, headers=headers, auth=auth, timeout=30).json()["access_token"]
    except Exception:
        return

    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.api+json"}

    def extract_list(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "results", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    def dump(label: str, url: str) -> None:
        print(f"  {label}:")
        try:
            r = session.get(url, headers=h, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"      (error de red: {exc})")
            return
        if r.status_code != 200:
            print(f"      (HTTP {r.status_code}: {r.text[:200]})")
            return
        try:
            payload = r.json()
        except Exception:
            print(f"      (respuesta no-JSON: {r.text[:200]})")
            return
        items = extract_list(payload)
        if not items:
            # Mostramos el crudo para ver la estructura real.
            print(f"      (lista vacía / estructura inesperada) RAW: {json.dumps(payload)[:400]}")
            return
        for it in items:
            a = it.get("attributes") or {}
            nm = it.get("name") or a.get("name") or a.get("label")
            idv = it.get("id") or a.get("id")
            print(f"      - {nm}" + (f" (id={idv})" if idv else ""))

    print("-" * 72)
    print("VALORES DISPONIBLES EN LA INSTANCIA (usá estos nombres EXACTOS en el .env):")
    dump("asset_type (IGAM_TABLET_TYPE / IGAM_PHONE_TYPE)", f"{IGAM.base_url}/public-api/asset-types/")
    dump("asset-status (IGAM_DEFAULT_STATUS_ID)", f"{IGAM.base_url}/public-api/asset-status/")
    print()
    print("CUSTOM FIELDS (poné estos 'id' en custom_fields.json -> { \"<id>\": \"<clave_WS1>\" }):")
    dump("custom-fields", f"{IGAM.base_url}/public-api/custom-fields/")
    print()
    print("LOCATIONS (para location_map.json -> { \"<grupo WS1>\": \"<id de acá>\" }):")
    dump("locations", f"{IGAM.base_url}/public-api/locations/")


def main() -> None:
    print("=" * 72)
    print("  Diagnóstico InvGate  -  OAuth2 token")
    print(f"  token_url     : {IGAM.token_url}")
    print(f"  grant_type    : {IGAM.grant_type}")
    print(f"  client_id     : {mask(IGAM.client_id)}")
    print(f"  client_secret : {mask(IGAM.client_secret)}")
    print("=" * 72)

    # Chequeo previo de credenciales vacías / sin completar.
    if IGAM.client_id in ("", "CHANGE_ME") or IGAM.client_secret in ("", "CHANGE_ME"):
        print("  ❌ client_id/client_secret NO están completados en el .env.")
        print("     Completá IGAM_CLIENT_ID e IGAM_CLIENT_SECRET y volvé a correr.")
        print("=" * 72)
        return

    print("Pruebas de estilo de autenticación:")
    ok_post = show("A) credenciales en el body (client_secret_post)", token_request(use_basic=False))
    ok_basic = show("B) credenciales en header Basic (client_secret_basic)", token_request(use_basic=True))

    if ok_post or ok_basic:
        _list_asset_types(use_basic=ok_basic and not ok_post)

    print("-" * 72)
    print("RECOMENDACIÓN:")
    if ok_post and ok_basic:
        print("  • Funcionan ambos. Dejá IGAM_AUTH_STYLE=auto (o post). Las credenciales están OK.")
    elif ok_post:
        print("  • Funciona SOLO el body -> IGAM_AUTH_STYLE=post  (auto también sirve).")
    elif ok_basic:
        print("  • Funciona SOLO Basic -> seteá  IGAM_AUTH_STYLE=basic  en el .env.")
    else:
        print("  • Fallan los dos estilos -> NO es el método, son las credenciales o la app:")
        print("     1) Revisá que IGAM_CLIENT_ID / IGAM_CLIENT_SECRET sean exactos (sin espacios ni comillas).")
        print("     2) En InvGate, la app OAuth debe tener habilitado grant_type 'client_credentials'.")
        print("     3) Confirmá la instancia: token_url debe ser tu dominio real de InvGate.")
        print(f"        Actual: {IGAM.token_url}")
    print("=" * 72)


if __name__ == "__main__":
    main()
