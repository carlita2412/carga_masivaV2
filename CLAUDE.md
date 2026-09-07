# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es este proyecto

Aplicación FastAPI (monolito server-rendered con Jinja2) para la carga masiva de datos de salud (beneficiarios, pesquisas antropométricas, pesquisas sanguíneas, signos vitales) hacia una base de datos MySQL, para el sistema Digisalud. Los usuarios suben archivos Excel con plantillas predefinidas y la app valida, transforma e inserta los datos.

## Comandos

Instalar dependencias:
```bash
pip install -r requirements.txt
```

Ejecutar en local (puerto 8000):
```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Ejecutar en servidor (puerto 8010, ver `comandos.txt`):
```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8010
```

No hay suite de tests ni linter configurado en este repo.

### Nginx / servicio (producción, en `comandos.txt`)
- Reiniciar: `nginx -s reload`
- Detener: `nginx -s stop`
- Verificar: `tasklist | findstr nginx`
- Nombre del servicio NSSM: `carga_masiva_uvicorn`

## Configuración

Variables de entorno en `.env` (no versionado): credenciales MySQL (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_PORT`) y un nombre de base de datos por país (`MYSQL_DB_VZLA`, `MYSQL_DB_COL`, `MYSQL_DB_ES`). `backend/db/db_connection.py:get_connection(pais)` abre una conexión pymysql nueva a la BD del país solicitado — no hay pool de conexiones ni ORM, todo es SQL crudo con placeholders `%s`.

La app se monta con `root_path="/carga_masiva"` (`backend/main.py:23`), pensado para correr detrás de un reverse proxy en ese subpath.

## Arquitectura

**`backend/main.py`** — único módulo de rutas FastAPI. Contiene:
- Login por sesión en memoria (dict `SESSIONS`, sin persistencia ni expiración) usando cookie `session`. Password hasheado con SHA1 contra `psi_usuarios` (organización fija `usuario_organizacion_id = 5`).
- Endpoints de carga masiva (`/api/cargar_excel*`), uno por tipo de dato: beneficiarios, pesquisa antropométrica, pesquisa sanguínea, vitales.
- Endpoints de descarga de plantillas Excel (`/descargas/*`) que sirven archivos desde `plantillas/`.
- Gestión de usuarios (`/usuarios/v2`) y jornadas (`/jornadas/v2`), vistas server-rendered con Jinja2.

**`backend/services/`** — un módulo `procesar_excel*` por tipo de carga (`carga_beneficiario.py`, `carga_pesquisas.py`, `carga_pesquisa_sanguineo.py`, `carga_vitales.py`). Cada uno recibe el DataFrame de pandas ya leído del Excel y devuelve un dict con las colecciones a insertar (personas, pacientes, escolaridades, autorizaciones, familiares o pesquisas) más una lista de `errores` por fila. **No insertan en BD directamente** — el endpoint en `main.py` hace las inserciones reales y decide commit/rollback.

**`backend/db/`**:
- `db_connection.py` — factory de conexiones por país.
- `queries.py` — funciones `insertar_*` (persona, paciente, escolaridad, autorizacion, familiar) con SQL parametrizado.
- `utils.py` — `obtener_persona_id_existente` (deduplicación de personas por `id_digisalud`/cédula/cédula escolar, lanza excepción si los identificadores apuntan a personas distintas) y `existe_persona_por_id`.

### Flujo de carga (patrón común a los 4 endpoints)
1. Leer el Excel subido con pandas (`header=1` o `header=0` según la plantilla).
2. Validar columnas obligatorias presentes.
3. Llamar al `procesar_excel*` correspondiente del servicio → arma listas de registros a insertar y detecta errores/duplicados sin tocar la BD.
4. Insertar registro por registro con SQL parametrizado, verificando duplicados existentes en BD antes de insertar.
5. Si hay errores de validación/inserción → `conn.rollback()`. Si todo va bien → `conn.commit()`.
6. Devuelve JSON con contadores de insertados, duplicados y errores (usado por el frontend para mostrar el resumen al usuario).

### Generación de IDs
`generar_persona_id()` en `carga_beneficiario.py` genera IDs únicos basados en timestamp en milisegundos + secuencia, protegido con un `Lock` para evitar colisiones. `generar_id_digisalud()` construye el identificador Digisalud a partir de nombre/apellido/género/país/fecha de nacimiento cuando no viene en la plantilla.

### Multi-país / multi-tabla
La actividad (`jornada` o `centro`) determina qué tablas se usan (`psi_pacientes_x_jornada` vs `psi_pacientes_x_centros`, `psi_pesquisas_x_paciente` vs `psi_pesquisas_x_centro`, etc.) — el nombre de tabla se arma dinámicamente con f-strings según el valor de `actividad`. El país (`vzla`, `colombia`, `elsalvador`) determina la base de datos MySQL destino.

## Frontend

`frontend/templates/` — HTML server-rendered con Jinja2, sin framework JS de por medio (jQuery/vanilla). `index.html` es el formulario principal de carga masiva; `usuarios/acciones_basev3.html` y `jornadas/jornadas_basev3.html` son las vistas de gestión. `frontend/static/` sirve assets estáticos (montado en `/static`).

## Plantillas

`plantillas/*.xlsx` son los archivos Excel de referencia que los usuarios descargan y llenan para la carga masiva; sus columnas deben coincidir exactamente con las validadas en cada endpoint de `main.py`. `plantillas/MANUAL_DESARROLLADOR.docx` y `Manual_de_Usuario.docx` documentan el proceso a nivel funcional.
