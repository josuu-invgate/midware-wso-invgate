"""
Configuración central de la integración Workspace ONE -> InvGate.

Lee variables de entorno (desde un archivo .env si existe) y las expone
mediante dos dataclasses: WS1Config e IGAMConfig.

Las URLs vienen con valores STANDARD; se sobreescriben con el .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    # Carga automática de .env si python-dotenv está instalado.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv es opcional
    pass


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _csv(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


# ---------------------------------------------------------------------------
#  WORKSPACE ONE (UEM / AirWatch) - API MDM de dispositivos
# ---------------------------------------------------------------------------
@dataclass
class WS1Config:
    base_url: str = os.getenv("WS1_BASE_URL", "https://as258.awmdm.com").rstrip("/")
    username: str = os.getenv("WS1_USERNAME", "CHANGE_ME")
    password: str = os.getenv("WS1_PASSWORD", "CHANGE_ME")
    tenant_code: str = os.getenv("WS1_TENANT_CODE", "CHANGE_ME")
    # Versión de la API: vacío => "application/json" (v1, la más completa para devices/search).
    # Si se setea (ej. "2") => "application/json;version=2".
    api_version: str = os.getenv("WS1_API_VERSION", "").strip()
    organization_group_id: Optional[str] = os.getenv("WS1_ORGANIZATION_GROUP_ID") or None
    platforms: List[str] = field(default_factory=lambda: _csv("WS1_PLATFORMS"))
    page_size: int = _int("WS1_PAGE_SIZE", 500)
    # Solo importar dispositivos efectivamente enrolados.
    only_enrolled: bool = _bool("WS1_ONLY_ENROLLED", True)

    @property
    def devices_search_url(self) -> str:
        # Endpoint STANDARD de la API MDM para buscar dispositivos enrolados.
        return f"{self.base_url}/api/mdm/devices/search"

    @property
    def accept_header(self) -> str:
        # v1 (sin sufijo) es la más completa para devices/search; v2 tiene gaps históricos.
        # Saneamos el valor: "V1"/"v1" -> "1" (un version mal formado como 'V1' rompe el server).
        ver = self.api_version.lstrip("vV").strip()
        return f"application/json;version={ver}" if ver else "application/json"


# ---------------------------------------------------------------------------
#  INVGATE ASSET MANAGEMENT - Public API (OAuth2 client_credentials)
# ---------------------------------------------------------------------------
@dataclass
class IGAMConfig:
    # .strip() para tolerar espacios/comillas accidentales en el .env.
    protocol: str = os.getenv("IGAM_PROTOCOL", "https").strip()
    instance_url: str = os.getenv("IGAM_INSTANCE_URL", "tu-instancia.is.cloud.invgate.net").strip().strip("/")
    port: str = os.getenv("IGAM_PORT", "443").strip()
    client_id: str = os.getenv("IGAM_CLIENT_ID", "CHANGE_ME").strip()
    client_secret: str = os.getenv("IGAM_CLIENT_SECRET", "CHANGE_ME").strip()
    grant_type: str = os.getenv("IGAM_GRANT_TYPE", "client_credentials").strip()
    # Estilo de auth del token: 'auto' (prueba body y si falla Basic), 'post' o 'basic'.
    auth_style: str = os.getenv("IGAM_AUTH_STYLE", "auto").strip().lower()

    # Nombres de asset_type: la API usa el NOMBRE NATIVO EN INGLÉS, no el traducido
    # del UI. El GET de un Phone mostró asset_type interno "N94" y resource type
    # "Phone"; el UI lo muestra "Telefone". "Tablet" funciona tal cual.
    tablet_type: str = os.getenv("IGAM_TABLET_TYPE", "Tablet").strip()
    phone_type: str = os.getenv("IGAM_PHONE_TYPE", "Phone").strip()

    # OJO: el id correcto depende de la instancia (ver GET /public-api/asset-status/).
    # En Riachuelo: 2=Active (1=Merged es interno, NO usar).
    default_status_id: str = os.getenv("IGAM_DEFAULT_STATUS_ID", "2").strip()
    default_location_id: Optional[str] = (os.getenv("IGAM_DEFAULT_LOCATION_ID") or "").strip() or None

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.instance_url}:{self.port}"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/oauth2/token/"

    @property
    def assets_lite_url(self) -> str:
        return f"{self.base_url}/public-api/assets-lite/"

    @property
    def custom_fields_list_url(self) -> str:
        return f"{self.base_url}/public-api/custom-fields/"

    @property
    def custom_field_value_url(self) -> str:
        # Endpoint v2 SINGULAR (un custom field por POST). NO lleva puerto (doc IGAM).
        return f"{self.protocol}://{self.instance_url}/public-api/v2/custom-field-value-cis/"

    @property
    def locations_url(self) -> str:
        return f"{self.base_url}/public-api/locations/"


# ---------------------------------------------------------------------------
#  Opciones globales de sincronización
# ---------------------------------------------------------------------------
@dataclass
class SyncConfig:
    dry_run: bool = _bool("DRY_RUN", True)
    update_existing: bool = _bool("UPDATE_EXISTING", True)
    # Resolver el owner buscando la Person por email (UserEmailAddress) en InvGate.
    # Si no encuentra match, omite el owner (no rompe). Default: off.
    resolve_owner: bool = _bool("RESOLVE_OWNER", False)
    # Verificar TLS de los endpoints (poné false solo para entornos de prueba).
    verify_tls: bool = _bool("VERIFY_TLS", True)


WS1 = WS1Config()
IGAM = IGAMConfig()
SYNC = SyncConfig()
