# Carga Masiva Digisalud (carga_masivaV2)

Aplicación web para la carga masiva de datos de salud —beneficiarios, pesquisas antropométricas, pesquisas sanguíneas y signos vitales— hacia la base de datos MySQL del sistema Digisalud. Los usuarios suben archivos Excel con plantillas predefinidas; la app valida, transforma e inserta los datos.

Monolito **FastAPI** con vistas server-rendered (**Jinja2**), sin framework JavaScript.

## Funcionalidades

| Módulo | Ruta | Qué hace |
|---|---|---|
| Carga masiva | `/carga` | Sube Excel de beneficiarios y pesquisas (antropometría, sanguíneo, sanguíneo avanzado, vitales) |
| Descargas | `/descargas/*` | Plantillas Excel y manual de usuario |
| Gestión de usuarios | `/usuarios/v2` | Bloquear usuarios, cambiar correo o contraseña |
| Gestión de jornadas | `/jornadas/v2` | Consultar jornadas; editar nombre y estatus |
| Copiar beneficiarios J-J | `/jornadas/v2/copiar` | Inscribe en una jornada a los beneficiarios de otra |
| Gestión de centros | `/centros/v2` | Consultar centros; editar nombre |
| Copiar beneficiarios J-C | `/centros/v2/copiar` | Inscribe en un centro a los beneficiarios de una jornada |
| Beneficiarios | `/beneficiarios/v2` | Consultar y corregir datos de identidad (recalcula el ID Digisalud) |

Todo se organiza por país (`vzla`, `colombia`, `elsalvador`) y por actividad (`jornada` o `centro`).

## Requisitos

- Python 3.12 o superior (el entorno actual usa 3.14)
- MySQL con una base de datos por país
- En producción: Nginx como proxy inverso y NSSM para ejecutar Uvicorn como servicio (Windows)

## Instalación

```bash
python -m venv .venv
# Git Bash
source .venv/Scripts/activate
# PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## Configuración

Crea un archivo `.env` en la raíz (no se versiona):

| Variable | Descripción |
|---|---|
| `MYSQL_HOST` | Servidor MySQL |
| `MYSQL_PORT` | Puerto (si falta, el código usa 33066) |
| `MYSQL_USER` / `MYSQL_PASSWORD` | Credenciales de la app (no usar `root`) |
| `MYSQL_DB_VZLA` | Base de datos de Venezuela |
| `MYSQL_DB_COL` | Base de datos de Colombia |
| `MYSQL_DB_ES` | Base de datos de El Salvador |
| `MAX_UPLOAD_SIZE_MB` | Opcional. Tamaño máximo de Excel (por defecto 20) |
| `CORS_ALLOWED_ORIGINS` | Opcional. Orígenes permitidos, separados por coma |

El `.env` se lee solo al arrancar: reinicia Uvicorn después de cambiarlo.

## Ejecución

| Entorno | Comando |
|---|---|
| Local (puerto 8000) | `uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload` |
| Servidor (puerto 8010) | `uvicorn backend.main:app --host 0.0.0.0 --port 8010` |

Abre `http://localhost:8000/login`. En producción la app vive bajo el subpath `/carga_masiva` (`root_path` de FastAPI) detrás de Nginx; el servicio de NSSM se llama `carga_masiva_uvicorn`.

Comandos de Nginx (Windows): `start nginx`, `nginx -s reload`, `nginx -s stop`, `tasklist | findstr nginx`.

## Estructura

```
carga_masivaV2/
├── backend/
│   ├── main.py                  # Todas las rutas FastAPI
│   ├── db/
│   │   ├── db_connection.py     # get_connection(pais)
│   │   ├── queries.py           # insertar_persona/paciente/escolaridad/...
│   │   └── utils.py             # deduplicación de personas
│   └── services/                # procesar_excel* por tipo de carga
├── frontend/
│   ├── static/                  # se sirve en /static
│   └── templates/               # Jinja2 (base, partials/sidebar, módulos)
├── plantillas/                  # Excel descargables y manuales (.docx)
├── comandos.txt                 # Comandos de servidor
└── requirements.txt
```

## Cómo funciona una carga

1. Se lee el Excel con pandas (`header=1` o `header=0` según la plantilla).
2. Se validan extensión, tamaño, firma binaria y columnas obligatorias.
3. El servicio `procesar_excel*` arma las listas de registros y detecta errores y duplicados, sin tocar la BD.
4. El endpoint inserta registro por registro con SQL parametrizado, verificando duplicados.
5. Si hay errores, `rollback`; si todo va bien, `commit`.
6. Responde JSON con contadores de insertados, duplicados y errores.

## Notas de desarrollo

- Todo el SQL es crudo con placeholders `%s`; no hay ORM ni pool de conexiones.
- Starlette 1.x exige `templates.TemplateResponse(request, "ruta.html", {...})`.
- Las sesiones viven en memoria: reiniciar Uvicorn cierra la sesión de todos y no escalan a varios workers.
- El sidebar es único (`frontend/templates/partials/sidebar.html`); cualquier opción nueva del menú va ahí.
- No hay suite de tests ni linter configurado.

## Documentación

| Documento | Contenido |
|---|---|
| `plantillas/Manual_de_Usuario.docx` | Uso funcional de cada pantalla |
| `plantillas/MANUAL_DESARROLLADOR.docx` | Arquitectura, módulos, rutas, seguridad y problemas comunes |
