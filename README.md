# Workspace ONE → InvGate Asset Management

Integração em Python que **importa dispositivos móveis (iOS/Android) do Workspace ONE UEM**
e os **cria/atualiza como ativos no InvGate Asset Management**.

> As URLs já vêm com valores *standard* — ajuste ao seu tenant no `.env`.

---

## Como funciona

```
Workspace ONE UEM                       InvGate Asset Management
GET /api/mdm/devices/search   ──►  mapeamento  ──►  POST/PATCH /public-api/assets-lite/
(dispositivos móveis)                              (+ custom fields, um por um)
```

1. Busca os dispositivos móveis enrolados no Workspace ONE (paginado, só plataformas móveis).
2. Mapeia cada dispositivo para o formato de ativo do InvGate.
3. **Deduplica por serial** (`GET /public-api/assets-lite/?serial=...`):
   - já existe + `UPDATE_EXISTING=true` → **PATCH** (atualização parcial, não sobrescreve dados curados à mão).
   - já existe + `UPDATE_EXISTING=false` → ignora.
   - não existe → **POST** (cria).
4. Depois de criar/atualizar, carrega os **custom fields** (RAM, MAC, IMEI…) um por um.
5. Imprime um resumo (criados / atualizados / ignorados / erros).

---

## Arquivos

| Arquivo                     | Função |
|-----------------------------|--------|
| `config.py`                 | Lê o `.env` e expõe a configuração (URLs *standard* + credenciais). |
| `workspace_one.py`          | Cliente Workspace ONE UEM (auth Basic + `aw-tenant-code`, busca paginada de devices). |
| `invgate.py`                | Cliente InvGate (OAuth2, buscar por serial, criar/atualizar, custom fields, owner, location). |
| `mapping.py`                | Converte um device do Workspace ONE no payload do InvGate (atributos + custom values). |
| `custom_fields.py`          | Carrega `custom_fields.json` e monta os custom fields a enviar. |
| `locations.py`              | Resolve a Location do InvGate a partir do grupo organizacional do WS1. |
| `sync_devices.py`           | Orquestrador (ponto de entrada). |
| `diagnose_ws1.py`           | Diagnóstico do Workspace ONE (scope, platform, paginação). |
| `diagnose_invgate.py`       | Diagnóstico do InvGate (token, asset-types, asset-status, custom-fields, locations). |
| `dump_sample.py`            | Volca em `samples/` um device cru + o request que iria ao InvGate (sem escrever nada). |
| `.env.example`              | Modelo de configuração — copie para `.env` e preencha. |
| `custom_fields.example.json`| Modelo do mapa de custom fields. |
| `location_map.example.json` | Modelo do mapa de locations. |

---

## Setup

```bash
# 1. (opcional) ambiente virtual
python -m venv .venv && .venv\Scripts\activate     # Windows
# source .venv/bin/activate                         # Linux/Mac

# 2. dependências
pip install -r requirements.txt

# 3. configuração
copy .env.example .env        # Windows  (cp no Linux/Mac)
#  -> editar o .env e preencher as credenciais
```

### Credenciais a preencher no `.env`

**Workspace ONE UEM**
- `WS1_BASE_URL` — host do tenant, ex. `https://as258.awmdm.com`.
- `WS1_USERNAME` / `WS1_PASSWORD` — usuário admin com permissão de API/Devices (auth Basic).
- `WS1_TENANT_CODE` — API Key (`aw-tenant-code`). Console: *Groups & Settings → All Settings → System → Advanced → API → REST API*.
- `WS1_ORGANIZATION_GROUP_ID` — Organization Group (parâmetro `lgid`). Em tenants grandes evita o erro 500.

**InvGate Asset Management** (OAuth2 `client_credentials`)
- `IGAM_INSTANCE_URL` — ex. `tua-instancia.is.cloud.invgate.net`.
- `IGAM_CLIENT_ID` / `IGAM_CLIENT_SECRET` — da aplicação OAuth2 registrada no InvGate.

---

## Uso

```bash
# Simulação (NÃO escreve nada) — recomendado na primeira vez.
python sync_devices.py

# Teste rápido escrevendo de verdade só os 5 primeiros
python sync_devices.py --test
python sync_devices.py --test --debug      # além disso, mostra os requests HTTP no console

# Escrever de verdade na frota inteira
python sync_devices.py --apply

# Forçar simulação independentemente do .env
python sync_devices.py --dry-run
```

A saída mostra, por dispositivo, se foi **criado** (`+`), **atualizado** (`~`) ou **ignorado** (`=`),
e fecha com um resumo.

---

## Logs e debugging

Cada execução grava um log completo em `logs/sync_<timestamp>.log`. Esse arquivo
**sempre** captura o detalhe HTTP: cada request a InvGate e Workspace ONE, com o
**body JSON enviado**, a resposta, e o token Bearer **mascarado** (credenciais nunca são logadas).

Para inspecionar o que é enviado, abra o `.log` e procure por `→ POST` / `request body` / `← 4xx`.

Diagnósticos isolados (não escrevem nada):
```bash
python diagnose_ws1.py        # Workspace ONE (500, scope/lgid, platform, paginação)
python diagnose_invgate.py    # InvGate (token OAuth2, asset-types, asset-status, custom-fields, locations)
python dump_sample.py         # volca em samples/ um device cru + o request ao InvGate
```

---

## Importante: o que o `assets-lite` realmente guarda

O POST de `assets-lite` SÓ persiste estes atributos: `name`, `serial`, `inventory_id`,
`asset_type`, `model`/`manufacturer`, `default_ip`, e os relationships `status`/`location`/`owner`.

Os campos técnicos (`ram`, `mac`, `imei`, `storage`, `os`…) são **read-only** pela API
(quem os preenche é o agente do InvGate) e são **ignorados** se enviados no body →
por isso vão por **custom fields**.

### Mapeamento de atributos (body do `assets-lite`)

| InvGate (`attributes`) | Workspace ONE | Observação |
|------------------------|---------------|-----------|
| `name`                 | `DeviceFriendlyName` → `DeviceReportedName` → `Model` | nunca fica vazio |
| `serial`               | `SerialNumber` → `Imei` → `Uuid` | também é a **chave de dedup** |
| `inventory_id`         | `AssetNumber` → `Udid` → `Uuid` | tag de inventário |
| `asset_type`           | derivado de `Model`/`Platform` | `Phone` ou `Tablet` (case-insensitive) |
| `model`                | `Model` | |
| `manufacturer`         | derivado de `OEMInfo`/`Model` | Apple, Samsung, Honeywell, Google… |
| `default_ip`           | rede do device | em geral não vem no `devices/search` |

Relacionamentos: `status` (de `IGAM_DEFAULT_STATUS_ID`, ex. 2=Ativo), `location` (ver abaixo),
`owner` (resolvendo `UserEmailAddress` → Person quando `RESOLVE_OWNER=true`).

---

## Custom fields (o resto dos dados)

Vão por `POST /public-api/v2/custom-field-value-cis/` (**um por POST**),
com body `{custom_field_id, ci_id, ci_type:"phone", value}`.

1. Crie os custom fields no InvGate (UI) para o tipo Phone.
2. Descubra os IDs: `python diagnose_invgate.py` (seção CUSTOM FIELDS).
3. Edite `custom_fields.json` no formato `{ "<id>": "<chave_WS1>" }`. As chaves vêm de
   `mapping.custom_values()`: `ram`, `storage_total`, `mac`, `ipv4`, `screen_size`, `imei`,
   `processor`, `phone_number`, `carrier`, `udid`, `os`, `enrollment_status`,
   `compliance_status`, `wifi_ssid`, `ownership_label`, `last_seen`, `user_email`, …
4. O `sync_devices.py` os carga UM POR UM depois de criar cada asset (usando o `ci_id` da
   resposta, respeitando `DRY_RUN` e tolerando falhas por campo). Valores vazios são omitidos.

> Observação: `ipv4`, `screen_size` e `processor` **não vêm** no `devices/search` — esses
> custom fields ficam vazios a partir desta fonte. `imei`, `storage_total`, `carrier` e
> `phone_number` aparecem em iOS / aparelhos com SIM.

---

## Location (por id, com tabela de tradução)

O grupo organizacional do WS1 (ex. "LOJAS SÃO PAULO") **não casa** com a Location do
InvGate (ex. "São Paulo"), então usa-se um mapa explícito:

1. Liste as locations: `python diagnose_invgate.py` (seção LOCATIONS).
2. Copie `location_map.example.json` → `location_map.json` no formato
   `{ "<LocationGroupId ou Name>": "<location_id do InvGate ou nome a resolver>" }`.
   - valor numérico → usado como `location_id` direto.
   - valor texto → resolvido por nome em `/public-api/locations/`.
3. Sem `location_map.json`, usa-se `IGAM_DEFAULT_LOCATION_ID` (ou nenhuma).

---

## Pontos a confirmar na sua instância InvGate

- **Tipos de ativo**: os nomes nativos costumam ser minúsculos (`phone`, `tablet`); o create
  é *case-insensitive*. Ajuste `IGAM_PHONE_TYPE` / `IGAM_TABLET_TYPE` no `.env` se diferirem.
- **Status**: confirme o id em `GET /public-api/asset-status/` (em Riachuelo 2=Ativo; **não** use 1=Merged).
- **Custom fields / Locations**: os IDs variam por tenant — pegue-os com `diagnose_invgate.py`.

---

## Notas técnicas

- **Workspace ONE**: um único *barrido* sem o parâmetro `platform` (evita o 204 e não percorre
  plataformas não-móveis); filtra móvel no cliente. Paginação base 0; `pagesize` 500.
- **Versão da API de devices**: v1 (`Accept: application/json`, `WS1_API_VERSION` vazio) é a mais
  completa; v2 tem *gaps*.
- **Token OAuth2**: cacheado e renovado ~60s antes de expirar; aceita credenciais por body ou Basic
  (`IGAM_AUTH_STYLE=auto`).
- **Robustez**: um dispositivo ou um custom field com erro **não aborta** o lote; é contabilizado e segue.

### Próximos passos opcionais
- Mapear mais custom fields (ver `mapping.custom_values`) ou completar o `location_map.json`.
- Sync incremental usando `seensince` / `lastseen` no Workspace ONE.
