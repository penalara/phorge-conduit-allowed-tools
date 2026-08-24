# Conduit MCP Server for Phabricator and Phorge

Conduit is a Model Context Protocol (MCP) server that exposes an administrator-selected allowlist of Phabricator/Phorge Conduit methods. It uses your Phorge API token to execute those methods and returns structured MCP responses.

## English

### Requirements

- Python 3.8 or newer.
- A Phabricator or Phorge instance with its Conduit API enabled.
- An API token. Tokens must be exactly 32 characters.
- `uv`/`uvx` is recommended, but `pip` is also supported.

### Installation

Run the published server with `uvx`:

```bash
uvx --from git+https://github.com/penalara/phorge-conduit-allowed-tools conduit-mcp --print-tools-config-path
```

Install from source for development:

```bash
git clone https://github.com/penalara/phorge-conduit-allowed-tools.git
cd phorge-conduit-allowed-tools
python -m venv venv
```

Linux and macOS:

```bash
source venv/bin/activate
pip install -e .[dev]
```

Windows PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Install with `pip` instead of editable mode:

```bash
pip install conduit-mcp
```

### Required Allowed-Methods File

Conduit does not start until an administrator creates `conduit-allowed-methods.json`. It intentionally does not ship or create a default allowlist during installation.

Create it from all methods visible to your API token:

Linux and macOS:

```bash
export PHABRICATOR_URL="https://phorge.example.com/api/"
export PHABRICATOR_TOKEN="your-32-character-token"
conduit-mcp --init-tools-config
```

Windows PowerShell:

```powershell
$env:PHABRICATOR_URL = "https://phorge.example.com/api/"
$env:PHABRICATOR_TOKEN = "your-32-character-token"
conduit-mcp --init-tools-config
```

The initialization command calls `conduit.query`, sorts all methods visible to the token, and writes them to the configuration file. Review and reduce this generated list before using it in production, especially if the token can modify data. The Conduit API does not report whether a method is deprecated, so the generated list can contain deprecated methods.

The file format is deliberately small:

```json
{
  "allowed_tools": [
    "phriction.content.search",
    "phriction.create",
    "phriction.document.search",
    "phriction.edit"
  ]
}
```

Names must be official Conduit method names. An empty list is valid and exposes no tools.

### Configuration Location

Use this command to print the effective path without requiring a token or an existing file:

```bash
conduit-mcp --print-tools-config-path
```

The default user paths are normally:

| Platform | Default path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\conduit-mcp\conduit-allowed-methods.json` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/conduit-mcp/conduit-allowed-methods.json` |
| macOS | `~/Library/Application Support/conduit-mcp/conduit-allowed-methods.json` |

`platformdirs` resolves the user directory, including `XDG_CONFIG_HOME` where applicable. Administrators may choose another path with the CLI option or environment variable:

```bash
conduit-mcp --tools-config /secure/path/conduit-allowed-methods.json
CONDUIT_TOOLS_CONFIG=/secure/path/conduit-allowed-methods.json conduit-mcp
```

CLI takes precedence over `CONDUIT_TOOLS_CONFIG`, which takes precedence over the default user path. `--init-tools-config` never overwrites an existing file unless `--force` is supplied:

```bash
conduit-mcp --init-tools-config --force
```

If the file is missing or invalid, Conduit exits before opening stdio or HTTP/SSE and prints setup instructions to standard error.

### Running The Server

Stdio mode requires URL, token, and a valid allowlist:

```bash
conduit-mcp
```

HTTP/SSE mode uses a token per request and still requires the allowlist at startup:

```bash
PHABRICATOR_URL="https://phorge.example.com/api/" conduit-mcp --host 127.0.0.1 --port 8000
```

Send the request token in this header:

```text
X-PHABRICATOR-TOKEN: your-32-character-token
```

Optional networking settings in both modes:

```bash
PHABRICATOR_PROXY="socks5://127.0.0.1:1080"
PHABRICATOR_DISABLE_CERT_VERIFY=1
```

Disabling certificate verification is unsafe and should only be used on a trusted network.

### Calling Tools

Every entry in `allowed_tools` becomes one MCP tool with the same method name. Pass native Conduit parameters inside one `params` object:

```json
{
  "params": {
    "constraints": {
      "paths": ["/documentation/"]
    },
    "limit": 100
  }
}
```

The method name is fixed when the server starts. Tool callers cannot override it, API tokens, or Conduit transport parameters.

The same allowlist is loaded by direct Python clients. Methods outside it fail
locally with `METHOD_NOT_ALLOWED` before any HTTP request. A missing or invalid
configuration is fail-closed. Only the administrative `--init-tools-config`
flow uses an explicit unrestricted client to call `conduit.query` and create
the initial configuration.

### Typed Tools

The server also registers typed high-level tools with names such as `pha_*`.
These tools are not entries in `allowed_tools`, but every Conduit method they
call internally is checked against the same allowlist. A typed tool fails with
`METHOD_NOT_ALLOWED` if one of its required Conduit methods is not allowed.

`pha_task_start` puts a Maniphest task in `En curso` in every associated
Workboard that has an exact matching column. It discovers the task tags and
columns automatically, reports Workboards without that column as `skipped`,
and performs one `maniphest.edit` only after completing its read precheck.

```json
{
  "task": "T123"
}
```

`task` is required and must match `T` followed by a positive integer. To use
`pha_task_start`, allow these underlying Conduit methods:

```json
{
  "allowed_tools": [
    "maniphest.search",
    "project.search",
    "project.column.search",
    "maniphest.edit"
  ]
}
```

`phorge_create_sprint` creates or patches the tasks in an already-loaded
sprint definition and creates its Phriction page only after a complete read
precheck and successful task writes. Clients provide the editable project and
owner-tag configuration; the MCP server does not read OpenCode skill files.

```json
{
  "source_path": "sprints/sprint-17.md",
  "source_text": "# Sprint 17\nBuild login;;@alice;High;Backend[Doing]",
  "project_config": {
    "name": "Team",
    "wikiBasePath": "projects/team/sprints/",
    "defaultTag": "Team"
  },
  "owner_sprint_tags": {
    "@alice": "Sprint Alice"
  }
}
```

`wikiBasePath` must be a safe relative Phriction path. Priorities must use the
exact English visible name returned by Phorge, such as `High`, `Normal`, or
`Low`. To use this tool, allow these underlying Conduit methods:

```json
{
  "allowed_tools": [
    "maniphest.search",
    "maniphest.priority.search",
    "maniphest.edit",
    "user.search",
    "project.search",
    "project.column.search",
    "phriction.document.search",
    "phriction.create"
  ]
}
```

### Security Notes

- The allowlist controls the entire MCP surface. Include write or administrative methods only when intended.
- Prefer an explicit `--tools-config` path with restricted filesystem permissions for shared services.
- Never place API tokens in `conduit-allowed-methods.json`.
- In SSE mode, a fresh client is created and closed for each request to isolate user tokens.

### Development And Tests

Integration tests require Docker and a local Phorge image:

```bash
cd tests
docker build -t phorge_debug .
docker run -d --rm -p 8080:80 --name phorge_debug phorge_debug
```

Obtain the token and run the suite from the repository root:

```bash
docker exec phorge_debug /usr/local/bin/get-api-token.sh
PHABRICATOR_TOKEN=<token> PHABRICATOR_URL=http://127.0.0.1:8080/api/ pytest
```

Run formatting and local checks with:

```bash
pre-commit run -a
```

## Documentacion en Castellano

### Requisitos

- Python 3.8 o superior.
- Una instancia de Phabricator o Phorge con la API Conduit habilitada.
- Un token API de exactamente 32 caracteres.
- Se recomienda `uv`/`uvx`, aunque tambien se puede usar `pip`.

### Instalacion

Ejecuta el servidor publicado con `uvx`:

```bash
uvx --from git+https://github.com/penalara/phorge-conduit-allowed-tools conduit-mcp --print-tools-config-path
```

Instalacion desde el codigo fuente para desarrollo:

```bash
git clone https://github.com/penalara/phorge-conduit-allowed-tools.git
cd phorge-conduit-allowed-tools
python -m venv venv
```

Linux y macOS:

```bash
source venv/bin/activate
pip install -e .[dev]
```

Windows PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Instalacion con `pip`, sin modo editable:

```bash
pip install conduit-mcp
```

### Archivo Obligatorio De Metodos Permitidos

Conduit no se inicia hasta que un administrador crea `conduit-allowed-methods.json`. No se distribuye ni se crea una allowlist predeterminada durante la instalacion.

Genera el archivo a partir de todos los metodos visibles para el token:

Linux y macOS:

```bash
export PHABRICATOR_URL="https://phorge.example.com/api/"
export PHABRICATOR_TOKEN="tu-token-de-32-caracteres"
conduit-mcp --init-tools-config
```

Windows PowerShell:

```powershell
$env:PHABRICATOR_URL = "https://phorge.example.com/api/"
$env:PHABRICATOR_TOKEN = "tu-token-de-32-caracteres"
conduit-mcp --init-tools-config
```

La inicializacion invoca `conduit.query`, ordena los metodos visibles para el token y los escribe en el archivo. Revisa y reduce la lista antes de usarla en produccion, especialmente si el token puede modificar datos. La API Conduit no informa de si un metodo esta deprecated, por lo que la lista generada puede contener metodos deprecated.

El formato es deliberadamente sencillo:

```json
{
  "allowed_tools": [
    "phriction.content.search",
    "phriction.create",
    "phriction.document.search",
    "phriction.edit"
  ]
}
```

Los nombres deben ser los nombres oficiales de los metodos Conduit. Una lista vacia es valida y no expone herramientas.

### Ubicacion De Configuracion

Este comando muestra la ruta efectiva sin requerir token ni archivo existente:

```bash
conduit-mcp --print-tools-config-path
```

Las rutas de usuario predeterminadas habituales son:

| Plataforma | Ruta predeterminada |
| --- | --- |
| Windows | `%LOCALAPPDATA%\conduit-mcp\conduit-allowed-methods.json` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/conduit-mcp/conduit-allowed-methods.json` |
| macOS | `~/Library/Application Support/conduit-mcp/conduit-allowed-methods.json` |

`platformdirs` resuelve la ruta de usuario, incluido `XDG_CONFIG_HOME` cuando corresponda. Los administradores pueden seleccionar otra ruta:

```bash
conduit-mcp --tools-config /ruta/segura/conduit-allowed-methods.json
CONDUIT_TOOLS_CONFIG=/ruta/segura/conduit-allowed-methods.json conduit-mcp
```

La CLI tiene prioridad sobre `CONDUIT_TOOLS_CONFIG`, que tiene prioridad sobre la ruta de usuario. `--init-tools-config` no sobrescribe un archivo existente salvo con `--force`:

```bash
conduit-mcp --init-tools-config --force
```

Si el archivo no existe o no es valido, Conduit termina antes de abrir stdio o HTTP/SSE y muestra instrucciones por error estandar.

### Ejecucion Del Servidor

El modo stdio requiere URL, token y una allowlist valida:

```bash
conduit-mcp
```

El modo HTTP/SSE usa un token por peticion y tambien requiere la allowlist al iniciar:

```bash
PHABRICATOR_URL="https://phorge.example.com/api/" conduit-mcp --host 127.0.0.1 --port 8000
```

Envia el token de la peticion en esta cabecera:

```text
X-PHABRICATOR-TOKEN: tu-token-de-32-caracteres
```

Configuracion opcional de red en ambos modos:

```bash
PHABRICATOR_PROXY="socks5://127.0.0.1:1080"
PHABRICATOR_DISABLE_CERT_VERIFY=1
```

Desactivar la verificacion de certificados es inseguro y solo debe hacerse en una red de confianza.

### Invocacion De Herramientas

Cada entrada de `allowed_tools` se convierte en una herramienta MCP con el mismo nombre. Los parametros nativos de Conduit se envian dentro de `params`:

```json
{
  "params": {
    "constraints": {
      "paths": ["/documentation/"]
    },
    "limit": 100
  }
}
```

El metodo queda fijado al iniciar el servidor. Quien invoca una herramienta no puede modificar el metodo, los tokens API ni parametros de transporte Conduit.

Los clientes Python directos cargan la misma allowlist. Los metodos que no
esten incluidos fallan localmente con `METHOD_NOT_ALLOWED` antes de realizar
ninguna peticion HTTP. Una configuracion ausente o invalida aplica fail-closed.
Solo el flujo administrativo `--init-tools-config` usa un cliente sin
restricciones de forma explicita para llamar a `conduit.query` y crear la
configuracion inicial.

### Herramientas Tipadas

El servidor tambien registra herramientas tipadas de alto nivel con nombres
como `pha_*`. Estas herramientas no se anaden a `allowed_tools`, pero cada
metodo Conduit que invocan internamente se valida contra la misma allowlist.
Una herramienta tipada falla con `METHOD_NOT_ALLOWED` si uno de sus metodos
Conduit requeridos no esta permitido.

`pha_task_start` coloca una tarea Maniphest en `En curso` en todos los
Workboards asociados que tengan una columna con ese nombre exacto. Descubre
automaticamente los tags y las columnas, informa los Workboards sin esa
columna en `skipped` y realiza un unico `maniphest.edit` solo despues de
completar el precheck de lectura.

```json
{
  "task": "T123"
}
```

`task` es obligatorio y debe tener una `T` seguida de un entero positivo. Para
usar `pha_task_start`, permite estos metodos Conduit internos:

```json
{
  "allowed_tools": [
    "maniphest.search",
    "project.search",
    "project.column.search",
    "maniphest.edit"
  ]
}
```

`phorge_create_sprint` crea o parchea las tareas de una definicion de sprint
ya cargada y solo crea su pagina Phriction despues de un precheck completo de
lectura y de completar correctamente las escrituras de tareas. El cliente
proporciona la configuracion editable de proyecto y tags por owner; el MCP no
lee archivos internos de Skills de OpenCode.

```json
{
  "source_path": "sprints/sprint-17.md",
  "source_text": "# Sprint 17\nCrear login;;@alice;High;Backend[Doing]",
  "project_config": {
    "name": "Equipo",
    "wikiBasePath": "projects/equipo/sprints/",
    "defaultTag": "Equipo"
  },
  "owner_sprint_tags": {
    "@alice": "Sprint Alice"
  }
}
```

`wikiBasePath` debe ser una ruta relativa segura de Phriction. Las prioridades
deben usar el nombre visible exacto en ingles devuelto por Phorge, como `High`,
`Normal` o `Low`. Para usar esta herramienta, permite estos metodos Conduit:

```json
{
  "allowed_tools": [
    "maniphest.search",
    "maniphest.priority.search",
    "maniphest.edit",
    "user.search",
    "project.search",
    "project.column.search",
    "phriction.document.search",
    "phriction.create"
  ]
}
```

### Notas De Seguridad

- La allowlist controla toda la superficie MCP. Incluye metodos de escritura o administrativos solo cuando sea intencionado.
- Para servicios compartidos, usa una ruta explicita con `--tools-config` y permisos de sistema de archivos restringidos.
- Nunca guardes tokens API en `conduit-allowed-methods.json`.
- En SSE se crea y cierra un cliente nuevo por peticion para aislar los tokens de usuarios.

### Desarrollo Y Pruebas

Las pruebas de integracion requieren Docker y una imagen local de Phorge:

```bash
cd tests
docker build -t phorge_debug .
docker run -d --rm -p 8080:80 --name phorge_debug phorge_debug
```

Obten el token y ejecuta las pruebas desde la raiz del repositorio:

```bash
docker exec phorge_debug /usr/local/bin/get-api-token.sh
PHABRICATOR_TOKEN=<token> PHABRICATOR_URL=http://127.0.0.1:8080/api/ pytest
```

Ejecuta formato y verificaciones locales con:

```bash
pre-commit run -a
```

## License

Licensed under the [MIT](LICENSE) license.
