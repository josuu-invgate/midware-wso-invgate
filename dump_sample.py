"""
Vuelca a la carpeta ./samples (para inspeccionar el mapeo y el request):

  - ws1_device_raw.json   : el JSON CRUDO de UN dispositivo de Workspace ONE.
  - invgate_request.json  : el BODY EXACTO que se enviaría a InvGate (POST assets-lite).
  - invgate_request.txt   : method + URL + headers + body, tal cual saldría.
  - mapping_explained.txt : tabla legible Workspace ONE -> InvGate.

NO escribe nada en InvGate ni necesita credenciales de InvGate: solo LEE de
Workspace ONE y arma el request localmente.

Uso:
    python dump_sample.py                # el primer dispositivo móvil
    python dump_sample.py --index 3      # el 4º dispositivo
    python dump_sample.py --serial 19260B88A1   # uno puntual por serial
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

import custom_fields
import mapping
from config import IGAM, SYNC, WS1
from invgate import JSON_API
from workspace_one import WorkspaceOneClient

SAMPLES = Path(__file__).resolve().parent / "samples"


def _enrich(ws1: WorkspaceOneClient, dev: Dict[str, Any]) -> Dict[str, Any]:
    """Agrega storage (detalle por id) si está habilitado y aún no vino."""
    if WS1.fetch_storage and "TotalStorageBytes" not in dev:
        dev = ws1.enrich_storage(dev)
    return dev


def _pick_device(serial: Optional[str], index: int) -> Optional[Dict[str, Any]]:
    WS1.page_size = min(max(WS1.page_size, 10), 100) if not serial else 100
    ws1 = WorkspaceOneClient(WS1, verify_tls=SYNC.verify_tls)

    # Búsqueda DIRECTA por serial (no depende del filtro móvil/enrolado, trae detalle).
    if serial:
        dev = ws1.get_device_by_serial(serial)
        if isinstance(dev, dict) and dev.get("SerialNumber"):
            print(f"  Encontrado por búsqueda directa (id={dev.get('Id')}, platform={dev.get('Platform')}).")
            return _enrich(ws1, dev)
        print("  (búsqueda directa por serial no devolvió nada; recorro el search...)")

    i = 0
    for dev in ws1.iter_mobile_devices():
        if not isinstance(dev, dict):
            continue
        if serial:
            if mapping.dedup_serial(dev) == serial or dev.get("SerialNumber") == serial:
                print(f"  Encontrado en el search (id={dev.get('Id')}, platform={dev.get('Platform')}).")
                return _enrich(ws1, dev)
        elif i == index:
            return _enrich(ws1, dev)
        i += 1
        # Cota de seguridad si buscamos por índice.
        if not serial and i > index:
            break
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Volcar muestra WS1 + request InvGate")
    parser.add_argument("--index", type=int, default=0, help="Índice del dispositivo (0 = primero)")
    parser.add_argument("--serial", default=None, help="Buscar un dispositivo por serial exacto")
    args = parser.parse_args()

    SAMPLES.mkdir(exist_ok=True)
    print(f"Carpeta de salida: {SAMPLES}")
    print(f"WS1: {WS1.base_url}  | fetch_storage={WS1.fetch_storage}")
    print(f"Buscando dispositivo (serial={args.serial or '-'}, index={args.index}) ...")

    try:
        dev = _pick_device(args.serial, args.index)
    except Exception as exc:  # noqa: BLE001 - mostramos el error real en vez de fallar mudo
        import traceback
        print("❌ ERROR al buscar el dispositivo:")
        traceback.print_exc()
        sys.exit(1)

    if not dev:
        print("❌ No se encontró ningún dispositivo con ese criterio.")
        print("   Revisá: el serial es exacto? el device está en este tenant? credenciales WS1 ok?")
        sys.exit(1)

    # 1) Crudo de Workspace ONE
    raw_path = SAMPLES / "ws1_device_raw.json"
    raw_path.write_text(json.dumps(dev, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) Mapeo -> body de InvGate (igual que en el sync real)
    attrs = mapping.device_to_attributes(dev)
    identifiers = mapping.custom_values(dev)
    payload = mapping.build_create_payload(
        attrs,
        status_id=IGAM.default_status_id,
        location_id=IGAM.default_location_id,
        owner_id=None,  # owner se resuelve por email en el sync; acá no lo tocamos
    )
    body_path = SAMPLES / "invgate_request.json"
    body_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) Request completo (como saldría por la red)
    req_txt = (
        f"POST {IGAM.assets_lite_url}\n"
        f"Headers:\n"
        f"  Authorization: Bearer <access_token>\n"
        f"  Accept: {JSON_API}\n"
        f"  Content-Type: {JSON_API}\n\n"
        f"Body:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )
    (SAMPLES / "invgate_request.txt").write_text(req_txt, encoding="utf-8")

    # 3b) Custom fields (si hay custom_fields.json): lo que iría al endpoint v2.
    cf_map = custom_fields.load_map()
    if cf_map:
        cf_items = custom_fields.build_items(dev, mapping.device_id(dev) or "<asset_id>", cf_map)
        (SAMPLES / "invgate_custom_fields.json").write_text(
            json.dumps(cf_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 4) Mapeo legible WS1 -> InvGate (todos los atributos mapeados, dinámico)
    lines = ["Workspace ONE  ->  InvGate (atributos mapeados)", "=" * 60]
    width = max((len(k) for k in attrs), default=12)
    for key, val in attrs.items():
        lines.append(f"  attributes.{key:<{width}} = {val}")
    lines.append(f"  status.id (del .env){'':<{max(0, width-16)}} = {IGAM.default_status_id}")
    lines += ["", "Valores para CUSTOM FIELDS (claves de custom_fields.json):"]
    width2 = max((len(k) for k in identifiers), default=14)
    for k, v in identifiers.items():
        lines.append(f"  {k:<{width2}} = {v}")
    (SAMPLES / "mapping_explained.txt").write_text("\n".join(lines), encoding="utf-8")

    # Resumen en consola
    generated = ["ws1_device_raw.json", "invgate_request.json", "invgate_request.txt", "mapping_explained.txt"]
    if cf_map:
        generated.append("invgate_custom_fields.json")
    print("\n✅ Archivos generados en ./samples :")
    for f in generated:
        print(f"   - samples/{f}")
    print("\n--- Resumen del mapeo ---")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
