"""
Orquestador: importa dispositivos móviles de Workspace ONE y los crea/actualiza
como assets en InvGate Asset Management.

Flujo:
  1. Trae dispositivos móviles desde Workspace ONE (GET /api/mdm/devices/search).
  2. Mapea cada device al formato de asset de InvGate.
  3. Deduplica por serial (GET /public-api/assets-lite/?serial=...).
       - existe + UPDATE_EXISTING -> PATCH (parcial, no pisa datos curados)
       - existe + !UPDATE_EXISTING -> omite
       - no existe -> POST (crea)
  4. Imprime un resumen.

Uso:
    python sync_devices.py                 # respeta DRY_RUN del .env
    python sync_devices.py --apply         # fuerza escritura real (DRY_RUN=false)
    python sync_devices.py --dry-run       # fuerza simulación
    python sync_devices.py --limit 5       # procesa solo los primeros 5 (para probar)
    python sync_devices.py --apply --limit 5   # escribe de verdad, solo 5
    python sync_devices.py --test          # atajo de lo anterior (= --apply --limit 5)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# La consola de Windows (cp1252) no soporta UTF-8 por defecto; forzamos UTF-8
# para no romper al imprimir acentos/símbolos.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

import custom_fields
import locations
import mapping
from config import IGAM, SYNC, WS1
from invgate import IGAMClient, IGAMError
from workspace_one import WorkspaceOneClient, WS1Error

logger = logging.getLogger("sync")


def setup_logging(debug: bool, log_file: Optional[str]) -> Path:
    """Configura logging a archivo (siempre DEBUG) y a consola (INFO, o DEBUG con --debug).

    El archivo SIEMPRE captura el detalle HTTP completo (requests/responses a InvGate
    y Workspace ONE), aunque en consola no se muestre.
    """
    logs_dir = Path(__file__).resolve().parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = logs_dir / path
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = logs_dir / f"sync_{ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):  # limpia handlers previos (re-ejecución)
        root.removeHandler(handler)

    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))  # consola limpia
    root.addHandler(ch)

    return path

# En UPDATE solo mandamos campos confiables desde WS1, para no pisar datos
# que un admin haya curado a mano en InvGate (inventory_id, location, owner...).
UPDATE_SAFE_ATTRS = ("name", "model", "manufacturer", "asset_type", "default_ip")


def _log(msg: str) -> None:
    logger.info(msg)


def _resolve_owner(igam: IGAMClient, identifiers: Dict[str, Any]) -> Optional[str]:
    if not SYNC.resolve_owner:
        return None
    return igam.find_person_by_email(identifiers.get("user_email") or "")


def _asset_type_name(item: Dict[str, Any]) -> str:
    """El nombre del tipo puede venir plano o anidado en attributes."""
    name = item.get("name")
    if name is None:
        name = (item.get("attributes") or {}).get("name")
    return (name or "").strip()


def _validate_asset_types(igam: IGAMClient) -> bool:
    """Verifica que los asset_type configurados existan en la instancia.
    Evita el 500 críptico de InvGate cuando el tipo no existe."""
    try:
        items = igam.list_asset_types()
    except IGAMError as exc:
        _log(f"  [!] No pude listar asset-types para validar (sigo igual): {exc}")
        return True
    names = {_asset_type_name(i) for i in items}
    names.discard("")
    if not names:
        # No pudimos leer la lista (estructura inesperada / vacía): NO bloqueamos.
        _log("  [!] No pude leer la lista de asset-types (sigo sin validar; revisá con diagnose_invgate.py).")
        return True
    # Comparación CASE-INSENSITIVE: la instancia lista los nativos en minúscula
    # ('phone', 'tablet') pero el create los acepta en cualquier caso ('Phone' -> 201).
    names_lower = {n.lower() for n in names}
    needed = {IGAM.tablet_type, IGAM.phone_type}
    missing = sorted(t for t in needed if t.lower() not in names_lower)
    if missing:
        _log(f"  [X] asset_type inexistente(s) en la instancia: {missing}")
        _log(f"      Tipos disponibles: {sorted(names)}")
        _log("      Ajustá IGAM_TABLET_TYPE / IGAM_PHONE_TYPE en el .env con los nombres exactos.")
        return False
    _log(f"  asset_type OK -> Tablet='{IGAM.tablet_type}', Phone='{IGAM.phone_type}'")
    return True


def _push_custom_fields(igam: IGAMClient, device: Dict[str, Any], ci_id: Any,
                        cf_map: Dict[str, str], label: str) -> None:
    """Carga los custom fields (datos WS1 sin atributo nativo) tras crear/actualizar."""
    if not cf_map:
        return
    items = custom_fields.build_items(device, ci_id or "<asset_id>", cf_map)
    if not items:
        return
    if SYNC.dry_run or not ci_id:
        _log(f"       [DRY-RUN] {len(items)} custom fields para {label}")
        return
    ok, fail = igam.set_custom_fields(items)
    suffix = f" ({fail} fallaron)" if fail else ""
    _log(f"       {ok} custom fields seteados para {label}{suffix}")


def _process_device(igam: IGAMClient, device: Dict[str, Any], stats: Dict[str, int],
                    cf_map: Dict[str, str]) -> None:
    attrs = mapping.device_to_attributes(device)
    identifiers = mapping.extract_identifiers(device)
    serial = attrs.get("serial")
    label = f"{attrs.get('name')} [{serial or 'sin-serial'}]"

    if not serial:
        _log(f"  [!]  OMITIDO (sin serial/imei/uuid): {attrs.get('name')}")
        stats["skipped"] += 1
        return

    # Dedup por serial.
    existing = igam.find_asset_by_serial(serial)
    owner_id = _resolve_owner(igam, identifiers)
    ci_id: Any = None

    if existing:
        if not SYNC.update_existing:
            _log(f"  =  YA EXISTE (omitido): {label}")
            stats["skipped"] += 1
            return
        asset_id = str(existing.get("id"))
        update_attrs = {k: v for k, v in attrs.items() if k in UPDATE_SAFE_ATTRS}
        payload = mapping.build_update_payload(asset_id, update_attrs, owner_id=owner_id)
        if SYNC.dry_run:
            _log(f"  ~  [DRY-RUN] PATCH asset {asset_id}: {label}")
            stats["would_update"] += 1
        else:
            igam.update_asset(asset_id, payload)
            _log(f"  ~  ACTUALIZADO asset {asset_id}: {label}")
            stats["updated"] += 1
        ci_id = asset_id
    else:
        location_id = locations.resolve(device, igam, IGAM.default_location_id)
        payload = mapping.build_create_payload(
            attrs,
            status_id=IGAM.default_status_id,
            location_id=location_id,
            owner_id=owner_id,
        )
        if SYNC.dry_run:
            _log(f"  +  [DRY-RUN] POST nuevo asset: {label}")
            stats["would_create"] += 1
        else:
            created = igam.create_asset(payload)
            ci_id = created.get("id")
            _log(f"  +  CREADO asset {ci_id}: {label}")
            stats["created"] += 1

    _push_custom_fields(igam, device, ci_id, cf_map, label)


def run(limit: Optional[int] = None) -> int:
    # Con límite, fijamos una página chica y razonable (independiente de
    # WS1_PAGE_SIZE) para que la prueba sea rápida: entre 10 y 100.
    if limit is not None and limit > 0:
        WS1.page_size = min(max(limit, 10), 100)

    mode = "DRY-RUN (no escribe)" if SYNC.dry_run else "APPLY (escribe en InvGate)"
    _log("=" * 70)
    _log("  Workspace ONE  ->  InvGate Asset Management")
    _log(f"  Modo: {mode}")
    _log(f"  WS1 devices/search : {WS1.devices_search_url}")
    _log(f"  InvGate assets-lite: {IGAM.assets_lite_url}")
    _log("=" * 70)

    ws1 = WorkspaceOneClient(WS1, verify_tls=SYNC.verify_tls)
    igam = IGAMClient(IGAM, verify_tls=SYNC.verify_tls)

    # Mapa opcional de custom fields (custom_fields.json). Vacío => no se cargan.
    cf_map = custom_fields.load_map()
    if cf_map:
        _log(f"  Custom fields configurados: {len(cf_map)} (custom_fields.json)")

    # Pre-chequeo: que los asset_type existan (evita 500 por cada device).
    if not _validate_asset_types(igam):
        if not SYNC.dry_run:
            _log("ABORTADO: corregí los asset_type antes de escribir en InvGate.")
            return 2
        _log("  (DRY-RUN: sigo, pero corregí los tipos antes de --apply.)")

    stats = {
        "total": 0, "created": 0, "updated": 0, "skipped": 0,
        "errors": 0, "would_create": 0, "would_update": 0,
    }

    try:
        devices = ws1.iter_mobile_devices()
    except WS1Error as exc:
        _log(f"[X] Error consultando Workspace ONE: {exc}")
        return 2

    for device in devices:
        if limit is not None and stats["total"] >= limit:
            break
        stats["total"] += 1
        try:
            # Storage (incl. Android) viene solo del detalle por device.
            if WS1.fetch_storage:
                device = ws1.enrich_storage(device)
            _process_device(igam, device, stats, cf_map)
        except IGAMError as exc:
            stats["errors"] += 1
            _log(f"  [X] ERROR InvGate: {exc}")
        except Exception as exc:  # noqa: BLE001 - un device roto no debe abortar el lote
            stats["errors"] += 1
            _log(f"  [X] ERROR inesperado: {exc}")

    _log("-" * 70)
    _log("RESUMEN")
    _log(f"  Dispositivos procesados : {stats['total']}")
    if SYNC.dry_run:
        _log(f"  Se crearían             : {stats['would_create']}")
        _log(f"  Se actualizarían        : {stats['would_update']}")
    else:
        _log(f"  Creados                 : {stats['created']}")
        _log(f"  Actualizados            : {stats['updated']}")
    _log(f"  Omitidos                : {stats['skipped']}")
    _log(f"  Errores                 : {stats['errors']}")
    _log("=" * 70)
    return 1 if stats["errors"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Workspace ONE -> InvGate")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="Fuerza escritura real (DRY_RUN=false)")
    group.add_argument("--dry-run", action="store_true", help="Fuerza simulación (DRY_RUN=true)")
    parser.add_argument("--limit", type=int, default=None, help="Procesar solo los primeros N devices")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Atajo de prueba: escribe DE VERDAD pero solo los primeros 5 (= --apply --limit 5)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Muestra en consola el detalle HTTP (requests/responses). El archivo de log SIEMPRE lo guarda.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Ruta del archivo de log (default: logs/sync_<timestamp>.log)",
    )
    args = parser.parse_args()

    log_path = setup_logging(args.debug, args.log_file)

    limit = args.limit
    if args.test:
        SYNC.dry_run = False
        if limit is None:
            limit = 5  # default del modo test; --limit lo puede sobreescribir
    elif args.apply:
        SYNC.dry_run = False
    elif args.dry_run:
        SYNC.dry_run = True

    logger.info("Log: %s", log_path)
    rc = run(limit=limit)
    logger.info("Log completo guardado en: %s", log_path)
    sys.exit(rc)


if __name__ == "__main__":
    main()
