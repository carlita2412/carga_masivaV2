from fastapi import FastAPI, Request, Query, UploadFile, File, Form, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse,FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import hashlib
import hmac
import logging
import os
import secrets
import time
import bcrypt
import pandas as pd
from io import BytesIO
from pathlib import Path
# Importaciones locales
from backend.db.db_connection import get_connection
from pymysql.err import IntegrityError
from backend.db.utils import obtener_persona_id_existente, existe_persona_por_id
from backend.services.carga_beneficiario import procesar_excel, generar_id_digisalud, obtener_cabecera
from backend.db.queries import (
    insertar_persona, insertar_paciente, insertar_escolaridad,
    insertar_autorizacion, insertar_familiar
)
from backend.services.carga_pesquisas import procesar_excel_pesquisa_antropometrica
from backend.services.carga_pesquisa_sanguineo import procesar_excel_pesquisa_sanguineo
from backend.services.carga_pesquisa_sanguineo_avanzada import procesar_excel_pesquisa_sanguineo_avanzada
from backend.services.carga_vitales import procesar_excel_vitales

app = FastAPI(root_path="/carga_masiva")
#app = FastAPI()

# Plantillas y estáticos
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend/templates"))
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# ---------------------------------------------------
# LÍMITE DE TAMAÑO DE ARCHIVOS SUBIDOS
# Los Excel de carga masiva no deberían superar unos pocos MB; un archivo
# gigante (accidental o deliberado) puede agotar la memoria del proceso al
# pasar por pandas/openpyxl. Configurable vía variable de entorno.
# NOTA: esta validación es defensa en profundidad a nivel de aplicación.
# El límite "duro" y confiable debe fijarse también en el reverse proxy
# (ej. `client_max_body_size` en Nginx), ya que un cliente puede mentir
# sobre el header Content-Length.
# ---------------------------------------------------
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


class LimiteTamanoSubidaMiddleware(BaseHTTPMiddleware):
    """Rechaza tempranamente, por Content-Length, los POST de carga de Excel
    que declaren un tamaño mayor al permitido. No sustituye el límite del
    reverse proxy, pero evita que un Content-Length honesto llegue a pandas."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/cargar_excel"):
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    declarado = int(content_length)
                except ValueError:
                    declarado = None
                # Margen para el overhead propio del multipart/form-data
                if declarado is not None and declarado > MAX_UPLOAD_SIZE_BYTES + (256 * 1024):
                    return JSONResponse(
                        status_code=413,
                        content={
                            "status": "error",
                            "mensaje": f"El archivo supera el tamaño máximo permitido ({MAX_UPLOAD_SIZE_MB} MB)."
                        }
                    )
        return await call_next(request)


app.add_middleware(LimiteTamanoSubidaMiddleware)

# ---------------------------------------------------
# CORS
# Esta es una app server-rendered (Jinja2): el frontend y la API viven en el
# mismo origen, y las peticiones normales del navegador (formularios, fetch
# desde el propio HTML servido por esta app) no requieren CORS en absoluto
# —CORS solo entra en juego si un origen *distinto* intenta leer la
# respuesta vía JS—. No hay ningún consumidor externo conocido, así que por
# defecto no se permite ningún origen cruzado. Si en el futuro se necesita
# exponer la API a otro dominio, agregarlo explícitamente vía la variable de
# entorno CORS_ALLOWED_ORIGINS (lista separada por comas, con protocolo,
# ej. "https://midominio.com,https://otro.midominio.com"). Nunca usar "*".
# ---------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    origen.strip() for origen in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if origen.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

EXTENSIONES_EXCEL_VALIDAS = (".xlsx", ".xls")
# Firmas de archivo (magic bytes): .xlsx es un ZIP, .xls (formato antiguo) es OLE Compound File.
FIRMAS_EXCEL_VALIDAS = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def _validar_archivo_excel(file: UploadFile, content: bytes):
    """Valida extensión, tamaño y firma binaria del archivo subido.
    Devuelve un mensaje de error (str) si es inválido, o None si está OK."""
    nombre = (file.filename or "").lower()
    if not nombre.endswith(EXTENSIONES_EXCEL_VALIDAS):
        return "El archivo debe tener extensión .xlsx o .xls"

    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        return f"El archivo supera el tamaño máximo permitido ({MAX_UPLOAD_SIZE_MB} MB)."

    if not content:
        return "El archivo está vacío."

    if not content.startswith(FIRMAS_EXCEL_VALIDAS):
        return "El archivo no parece ser un Excel válido (.xlsx/.xls)."

    return None

# Sesiones temporales en memoria.
# Cada valor es {"username": ..., "user_id": ..., "expira": epoch_seconds}
SESSIONS = {}
SESSION_TTL_SEGUNDOS = 8 * 60 * 60  # 8 horas de sesión

# Valores permitidos para "actividad": se usan para construir nombres de
# tabla/columna en SQL dinámico, por lo que NUNCA deben aceptarse tal cual
# vengan del cliente sin pasar por esta whitelist.
ACTIVIDADES_VALIDAS = {"jornada", "centro"}


# ---------------------------------------------------
# HASHING DE CONTRASEÑAS
# Los usuarios existentes tienen su contraseña en SHA1 (heredado). La
# verificación soporta SHA1 y bcrypt; el login NO modifica el hash
# almacenado. Las contraseñas restablecidas desde la gestión de usuarios se
# guardan en SHA1, que es el formato que espera Digisalud (BD compartida).
# ---------------------------------------------------

def _hash_password_sha1(password: str) -> str:
    """Hash que se escribe en psi_usuarios (compartida con Digisalud)."""
    return hashlib.sha1(password.encode("utf-8")).hexdigest()


def _es_hash_bcrypt(valor: str) -> bool:
    return bool(valor) and valor.startswith(("$2a$", "$2b$", "$2y$"))


def _password_coincide(password: str, hash_almacenado: str) -> bool:
    """Verifica el password contra el hash almacenado: SHA1 (formato de
    Digisalud) o bcrypt (usuarios que ya fueron migrados por error).
    Comparación en tiempo constante."""
    if not hash_almacenado:
        return False
    if _es_hash_bcrypt(hash_almacenado):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hash_almacenado.encode("utf-8"))
        except ValueError:
            return False
    return hmac.compare_digest(_hash_password_sha1(password), hash_almacenado.strip().lower())


# ---------------------------------------------------
# SESIONES
# ---------------------------------------------------

def _crear_sesion(user_id, username: str) -> str:
    """Genera un token de sesión aleatorio (no predecible) y lo registra."""
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "username": username,
        "user_id": user_id,
        "expira": time.time() + SESSION_TTL_SEGUNDOS,
    }
    return token


def _usuario_autenticado(request: Request):
    """Devuelve el username de la sesión activa, o None si no hay sesión
    válida o si expiró (en cuyo caso también la elimina)."""
    session_token = request.cookies.get("session")
    if not session_token:
        return None

    sesion = SESSIONS.get(session_token)
    if not sesion:
        return None

    if time.time() > sesion["expira"]:
        SESSIONS.pop(session_token, None)
        return None

    return sesion["username"]


def _respuesta_no_autenticado():
    return JSONResponse(status_code=401, content={"status": "error", "mensaje": "No autenticado"})


def _respuesta_actividad_invalida():
    return JSONResponse(
        status_code=400,
        content={"status": "error", "mensaje": "Actividad no válida. Debe ser 'jornada' o 'centro'."}
    )


# ---------------------------------------------------
# MANEJO DE ERRORES INESPERADOS
# El detalle completo de la excepción (que puede incluir fragmentos de SQL,
# nombres de tabla/columna, rutas internas, etc.) se registra solo en el log
# del servidor. Al cliente se le devuelve un mensaje genérico, para no
# facilitar reconocimiento de la infraestructura interna (CWE-209).
# ---------------------------------------------------
logger = logging.getLogger("carga_masiva.api")
MENSAJE_ERROR_GENERICO = "Ocurrió un error al procesar el archivo. Contacte al administrador del sistema."


def _log_y_responder_error(e: Exception, contexto: str):
    logger.exception("Error inesperado en %s", contexto)
    return {"status": "error", "mensaje": MENSAJE_ERROR_GENERICO}

# ---------------------------------------------------
# RUTAS DE LOGIN Y SESIÓN
# ---------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if _usuario_autenticado(request):
        return RedirectResponse(url="/carga", status_code=302)
    return RedirectResponse(url="/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def show_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        conn = get_connection("vzla")
    except Exception:
        logger.exception("No se pudo conectar a la base de datos en el login")
        return templates.TemplateResponse(request, "login.html", {
            "request": request,
            "error": "No se pudo conectar a la base de datos. Contacte al administrador del sistema."
        }, status_code=503)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT usuario_id, usuario_password FROM psi_usuarios
        WHERE usuario_username = %s
          AND usuario_status = 0
          AND usuario_organizacion_id = 5
    """, (username,))
    result = cursor.fetchone()

    if result and _password_coincide(password, result[1]):
        user_id = result[0]

        # No se re-hashea el password en el login: psi_usuarios es compartida
        # con otros sistemas que validan SHA1, y migrar aquí los dejaba fuera.
        conn.close()

        session_token = _crear_sesion(user_id, username)

        # Deja lista en segundo plano la lista de «Sin actividad» (tarda unos segundos en calcularse); no bloquea el login.
        huerfanos.precalentar(sorted(PAISES_VALIDOS), get_connection)

        response = RedirectResponse(url="/carga", status_code=302)
        response.set_cookie(
            key="session",
            value=session_token,
            httponly=True,
            samesite="lax",
            path="/"
        )
        return response

    conn.close()
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "error": "Credenciales incorrectas"
    })

@app.get("/logout")
async def logout(request: Request):
    session_token = request.cookies.get("session")
    if session_token:
        SESSIONS.pop(session_token, None)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response

@app.get("/carga", response_class=HTMLResponse)
async def carga_masiva(request: Request):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "index.html", {"request": request, "usuario": usuario})

# ---------------------------------------------------
# API DE OPCIONES Y CARGAS
# ---------------------------------------------------

@app.get("/api/opciones")
async def get_opciones(request: Request, pais: str = Query(...), actividad: str = Query(...)):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    actividad = actividad.lower().strip()
    if actividad not in ACTIVIDADES_VALIDAS:
        return _respuesta_actividad_invalida()

    conn = get_connection(pais)
    cursor = conn.cursor()

    if actividad == "centro":
        sql = """
        SELECT a.centro_id, a.centro_nombre, b.institucion_id
        FROM psi_centros a
        INNER JOIN psi_instituciones_x_centro b ON a.centro_id = b.centro_id
        ORDER BY a.centro_id DESC
        """
    elif actividad == "jornada":
        sql = """
        SELECT a.jornada_id, a.jornada_nombre, b.institucion_id
        FROM psi_jornadas a
        INNER JOIN psi_instituciones_x_jornada b ON a.jornada_id = b.jornada_id
        ORDER BY  a.jornada_id DESC
        """
    else:
        return JSONResponse(status_code=400, content={"error": "Actividad no válida"})

    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()

    return [{"id": r[0], "nombre": r[1], "institucion_id": r[2]} for r in rows]
#carga beneficiarios endpoint
@app.post("/api/cargar_excel")
async def cargar_excel(
    request: Request,
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...),
    institucion_id: int = Form(...)
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    conn = None
    try:
        actividad = actividad.lower().strip()
        if actividad not in ACTIVIDADES_VALIDAS:
            return {"status": "error", "mensaje": "Actividad no válida. Debe ser 'jornada' o 'centro'."}

        # ---------------------------------------------------------------------
        # Leer Excel
        # header=1 porque la plantilla tiene encabezados en la fila 2
        # ---------------------------------------------------------------------
        # Se limita la lectura a MAX_UPLOAD_SIZE_BYTES + 1 para no cargar en
        # memoria un archivo arbitrariamente grande solo para detectarlo.
        content = await file.read(MAX_UPLOAD_SIZE_BYTES + 1)
        error_archivo = _validar_archivo_excel(file, content)
        if error_archivo:
            return {"status": "error", "mensaje": error_archivo}
        df = pd.read_excel(BytesIO(content), header=1)
        df = df.dropna(how='all').reset_index(drop=True)

        # ---------------------------------------------------------------------
        # Abrir conexión
        # ---------------------------------------------------------------------
        conn = get_connection(pais)
        cursor = conn.cursor()

        # ---------------------------------------------------------------------
        # Procesar Excel:
        # - personas nuevas
        # - personas ya existentes
        # - pacientes
        # - escolaridades
        # - autorizaciones
        # - familiares
        # ---------------------------------------------------------------------
        resultado = procesar_excel(df, pais, actividad, destino_id, institucion_id, cursor)

        # ---------------------------------------------------------------------
        # personas_existentes:
        # son las personas que ya estaban en BD y sí deben poder ser asociadas
        # a la actividad sin volverlas a insertar.
        # ---------------------------------------------------------------------
        ids_persona_confirmados = set(resultado.get("personas_existentes", []))

        duplicados = []
        errores = list(resultado.get("errores", []))

        # Contadores reales de inserción
        insertados_personas = 0
        insertados_pacientes = 0
        insertados_escolaridades = 0
        insertados_autorizaciones = 0
        insertados_familiares = 0

        # =====================================================================
        # 1) INSERTAR PERSONAS NUEVAS
        # Si ya existen en BD, se reutiliza su persona_id.
        # Si no existen, se insertan.
        # =====================================================================
        for persona in resultado["personas"]:
            try:
                persona_id_existente = obtener_persona_id_existente(
                    cursor,
                    id_digisalud=persona.get("id_digisalud"),
                    cedula=persona.get("persona_cedula"),
                    cedula_escolar=persona.get("persona_cedula_escolar")
                )

                if persona_id_existente:
                    persona["persona_id"] = persona_id_existente
                    ids_persona_confirmados.add(persona_id_existente)
                    duplicados.append(
                        f"Ya existía persona: {persona['persona_nombre']} {persona['persona_apellido']}"
                    )
                else:
                    insertar_persona(cursor, persona)

                    if cursor.rowcount != 1:
                        raise Exception(f"No se insertó la persona {persona['id_digisalud']}")

                    ids_persona_confirmados.add(persona["persona_id"])
                    insertados_personas += 1

            except IntegrityError as e:
                errores.append(
                    f"IntegrityError insertando persona {persona.get('id_digisalud')}: {str(e)}"
                )
            except Exception as e:
                errores.append(
                    f"Error insertando persona {persona.get('id_digisalud')}: {str(e)}"
                )

        # Si ya falló algo en personas, no seguimos
        if errores:
            print("ERRORES DETECTADOS EN LA CARGA:")
            for err in errores:
                print("-", err)

            conn.rollback()
            return {
                "status": "error",
                "mensaje": "Falló la inserción o validación de una o más personas",
                "errores": errores,
                "duplicados": sorted(set(duplicados))
            }

        # =====================================================================
        # 2) INSERTAR PACIENTES / ASOCIAR A LA ACTIVIDAD
        # Aquí es donde se corrige tu caso:
        # - si la persona ya existía, también se asocia a la jornada/centro
        # =====================================================================
        tabla_pacientes = "psi_pacientes_x_jornada" if actividad == "jornada" else "psi_pacientes_x_centros"
        campo_id_actividad = f"{actividad}_id"

        for pac in resultado["pacientes"]:
            persona_id = pac["persona_id"]

            # Si no estaba confirmada aún, se verifica en BD por persona_id
            if persona_id not in ids_persona_confirmados:
                if existe_persona_por_id(cursor, persona_id):
                    ids_persona_confirmados.add(persona_id)
                else:
                    errores.append(f"No existe persona confirmada para persona_id={persona_id}")
                    continue

            # Verificar si ya está asociada a la actividad
            cursor.execute(
                f"""
                SELECT 1
                FROM {tabla_pacientes}
                WHERE persona_id = %s
                  AND {campo_id_actividad} = %s
                LIMIT 1
                """,
                (persona_id, pac[campo_id_actividad])
            )

            if cursor.fetchone():
                duplicados.append(
                    f"Ya estaba asociado a la {actividad}: persona_id={persona_id}"
                )
                continue

            try:
                insertar_paciente(cursor, pac, actividad)
                insertados_pacientes += 1
            except IntegrityError:
                duplicados.append(f"Paciente duplicado: persona_id={persona_id}")
            except Exception as e:
                errores.append(f"Error insertando paciente {persona_id}: {str(e)}")

        # =====================================================================
        # 3) AUTORIZACIONES
        # =====================================================================
        tabla_aut = "psi_aut_pac_x_jornada" if actividad == "jornada" else "psi_aut_pac_x_centro"

        for au in resultado["autorizaciones"]:
            persona_id = au["persona_id"]

            if persona_id not in ids_persona_confirmados:
                if existe_persona_por_id(cursor, persona_id):
                    ids_persona_confirmados.add(persona_id)
                else:
                    errores.append(f"No existe persona confirmada para autorización: persona_id={persona_id}")
                    continue

            cursor.execute(
                f"""
                SELECT 1
                FROM {tabla_aut}
                WHERE persona_id = %s
                  AND {campo_id_actividad} = %s
                  AND autorizacion_id = %s
                LIMIT 1
                """,
                (persona_id, au[campo_id_actividad], au["autorizacion_id"])
            )

            if cursor.fetchone():
                duplicados.append(
                    f"Autorización ya existente: persona_id={persona_id}, autorizacion_id={au['autorizacion_id']}"
                )
                continue

            try:
                insertar_autorizacion(cursor, au, actividad)
                insertados_autorizaciones += 1
            except IntegrityError:
                duplicados.append(
                    f"Autorización duplicada: persona_id={persona_id}, autorizacion_id={au['autorizacion_id']}"
                )
            except Exception as e:
                errores.append(f"Error insertando autorización {persona_id}: {str(e)}")

        # =====================================================================
        # 4) ESCOLARIDAD
        # =====================================================================
        tabla_esc = "psi_escolaridad" if actividad == "jornada" else "psi_escolaridad_centro"

        for es in resultado["escolaridades"]:
            persona_id = es["persona_id"]

            if persona_id not in ids_persona_confirmados:
                if existe_persona_por_id(cursor, persona_id):
                    ids_persona_confirmados.add(persona_id)
                else:
                    errores.append(f"No existe persona confirmada para escolaridad: persona_id={persona_id}")
                    continue

            # Verificación previa para evitar duplicados obvios en la misma actividad
            cursor.execute(
                f"""
                SELECT 1
                FROM {tabla_esc}
                WHERE persona_id = %s
                  AND {campo_id_actividad} = %s
                  AND COALESCE(escolaridad_grado, '') = COALESCE(%s, '')
                  AND COALESCE(escolaridad_seccion, '') = COALESCE(%s, '')
                  AND COALESCE(escolaridad_turno, '') = COALESCE(%s, '')
                  AND COALESCE(escolaridad_escuela, '') = COALESCE(%s, '')
                LIMIT 1
                """,
                (
                    persona_id,
                    es[campo_id_actividad],
                    es.get("escolaridad_grado"),
                    es.get("escolaridad_seccion"),
                    es.get("escolaridad_turno"),
                    es.get("escolaridad_escuela")
                )
            )

            if cursor.fetchone():
                duplicados.append(f"Escolaridad ya existente: persona_id={persona_id}")
                continue

            try:
                insertar_escolaridad(cursor, es, actividad)
                insertados_escolaridades += 1
            except IntegrityError as e:
                duplicados.append(f"Escolaridad duplicada: persona_id={persona_id} - {str(e)}")
            except Exception as e:
                errores.append(f"Error insertando escolaridad {persona_id}: {str(e)}")

        # =====================================================================
        # 5) FAMILIARES
        # =====================================================================
        for fam in resultado["familiares"]:
            a = fam["persona_id_A"]
            b = fam["persona_id_B"]

            # Confirmar ambos lados de la relación
            if a not in ids_persona_confirmados:
                if existe_persona_por_id(cursor, a):
                    ids_persona_confirmados.add(a)
                else:
                    errores.append(f"No existe persona confirmada para familiar A={a}")
                    continue

            if b not in ids_persona_confirmados:
                if existe_persona_por_id(cursor, b):
                    ids_persona_confirmados.add(b)
                else:
                    errores.append(f"No existe persona confirmada para familiar B={b}")
                    continue

            cursor.execute(
                """
                SELECT 1
                FROM psi_familiares
                WHERE persona_id_A = %s
                  AND persona_id_B = %s
                  AND parentesco_id = %s
                LIMIT 1
                """,
                (a, b, fam["parentesco_id"])
            )

            if cursor.fetchone():
                duplicados.append(f"Relación familiar ya existente: A={a}, B={b}")
                continue

            try:
                insertar_familiar(cursor, fam)
                insertados_familiares += 1
            except IntegrityError:
                duplicados.append(f"Familiar duplicado: A={a}, B={b}")
            except Exception as e:
                errores.append(f"Error insertando familiar A={a}, B={b}: {str(e)}")

        # =====================================================================
        # 6) SI HAY ERRORES -> ROLLBACK
        # =====================================================================
        if errores:
            print("ERRORES DETECTADOS EN LA CARGA:")
            for err in errores:
                print("-", err)

            conn.rollback()
            return {
                "status": "error",
                "mensaje": "Se encontraron errores durante la carga",
                "errores": errores,
                "duplicados": sorted(set(duplicados))
            }

        # =====================================================================
        # 7) TODO OK -> COMMIT
        # =====================================================================
        conn.commit()

        return {
            "status": "ok",
            "mensaje": "Carga procesada correctamente",
            "insertados": {
                "personas_nuevas": insertados_personas,
                "pacientes_asociados": insertados_pacientes,
                "escolaridades": insertados_escolaridades,
                "autorizaciones": insertados_autorizaciones,
                "familiares": insertados_familiares
            },
            "personas_existentes_reutilizadas": len(resultado.get("personas_existentes", [])),
            "duplicados": sorted(set(duplicados)),
            "errores": []
        }

    except Exception as e:
        if conn:
            conn.rollback()

        return _log_y_responder_error(e, "cargar_excel")

    finally:
        if conn:
            conn.close()
#carga antropometria
from fastapi import UploadFile, File, Form
import pandas as pd
from io import BytesIO

from backend.db.db_connection import get_connection
from backend.services.carga_pesquisas import procesar_excel_pesquisa_antropometrica

from fastapi import UploadFile, File, Form
import pandas as pd
from io import BytesIO

from backend.db.db_connection import get_connection
from backend.services.carga_pesquisas import procesar_excel_pesquisa_antropometrica


def es_error_duplicado_mysql(exc: Exception) -> bool:
    try:
        if hasattr(exc, "args") and exc.args:
            return int(exc.args[0]) == 1062
    except Exception:
        pass
    return "Duplicate entry" in str(exc)


@app.post("/api/cargar_excel_pesquisa_antropometrica")
async def cargar_excel_pesquisa_antropometrica(
    request: Request,
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    conn = None
    cursor = None

    try:
        actividad = actividad.lower().strip()
        if actividad not in ACTIVIDADES_VALIDAS:
            return {"status": "error", "mensaje": "Actividad no válida. Debe ser 'jornada' o 'centro'."}

        # Se limita la lectura a MAX_UPLOAD_SIZE_BYTES + 1 para no cargar en
        # memoria un archivo arbitrariamente grande solo para detectarlo.
        content = await file.read(MAX_UPLOAD_SIZE_BYTES + 1)
        error_archivo = _validar_archivo_excel(file, content)
        if error_archivo:
            return {"status": "error", "mensaje": error_archivo}
        df = pd.read_excel(BytesIO(content), header=1)

        # Los usuarios a veces renombran la columna como "Observaciones" o
        # "observacion": se normaliza (sin importar mayúsculas, plural ni espacios).
        df = df.rename(columns={
            col: "OBSERVACION"
            for col in df.columns
            if str(col).strip().upper() in ("OBSERVACION", "OBSERVACIONES")
        })

        columnas_obligatorias = [
            "Id Digisalud Beneficiario",
            "PESO",
            "TALLA",
            "Fecha Eval. DD/MM/AAAA",
            "OBSERVACION"
        ]

        for col in columnas_obligatorias:
            if col not in df.columns:
                return {
                    "status": "error",
                    "mensaje": f"Falta columna obligatoria: '{col}'"
                }

        try:
            df["Fecha Eval. DD/MM/AAAA"] = pd.to_datetime(
                df["Fecha Eval. DD/MM/AAAA"],
                format="%d/%m/%Y"
            )
        except Exception as e:
            return {
                "status": "error",
                "mensaje": f"Formato de fecha inválido en 'Fecha Eval. DD/MM/AAAA': {e}"
            }

        resultado = procesar_excel_pesquisa_antropometrica(df, pais, actividad, destino_id)

        conn = get_connection(pais)
        cursor = conn.cursor()

        tabla = "psi_pesquisas_x_paciente" if actividad == "jornada" else "psi_pesquisas_x_centro"
        id_campo = "jornada_id" if actividad == "jornada" else "centro_id"
        fecha_campo = "pesq_x_pac_fecha_evauacion"

        sql = f"""
            INSERT INTO {tabla} (
                persona_id,
                {id_campo},
                tipo_pesquisa_id,
                pesq_x_pac_valor,
                {fecha_campo},
                control_usuario_creacion,
                control_fecha_creacion
            )
            VALUES (%s, %s, %s, %s, %s, %s, CURDATE())
        """

        errores = list(resultado["errores"])
        beneficiarios_insertados = 0
        total_pesquisas_insertadas = 0

        for beneficiario in resultado["beneficiarios"]:
            try:
                for r in beneficiario["pesquisas"]:
                    data = (
                        r["persona_id"],
                        r[id_campo],
                        r["tipo_pesquisa_id"],
                        r["pesquisa_valor"],
                        r["pesquisa_fecha"],
                        1522702145282
                    )
                    cursor.execute(sql, data)

                conn.commit()
                beneficiarios_insertados += 1
                total_pesquisas_insertadas += len(beneficiario["pesquisas"])

            except Exception as e:
                conn.rollback()

                nombre_completo = f"{beneficiario['persona_nombre']} {beneficiario['persona_apellido']}".strip()
                fila_excel = beneficiario["fila_excel"]

                if es_error_duplicado_mysql(e):
                    errores.append(
                        f"Fila {fila_excel}: El beneficiario '{nombre_completo}' ya tiene una evaluación antropométrica registrada en esta {actividad}. "
                        f"Se omitió ese registro y la carga continuó con los demás beneficiarios."
                    )
                else:
                    errores.append(
                        f"Fila {fila_excel}: Error al guardar al beneficiario '{nombre_completo}'. Detalle: {str(e)}"
                    )

        mensaje = "Carga completada correctamente."
        if errores:
            mensaje = "Carga completada con observaciones."

        return {
            "status": "ok",
            "mensaje": mensaje,
            "insertados": beneficiarios_insertados,  # compatibilidad con frontend anterior
            "beneficiarios_insertados": beneficiarios_insertados,
            "pesquisas_insertadas": total_pesquisas_insertadas,
            "errores": errores
        }

    except Exception as e:
        if conn:
            conn.rollback()

        return _log_y_responder_error(e, "cargar_excel_pesquisa_antropometrica")

    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass

        try:
            if conn:
                conn.close()
        except Exception:
            pass
#CARGA PESQUISA SANGUINEO

@app.post("/api/cargar_excel_pesquisa_sanguineo")
async def cargar_excel_pesquisa_sanguineo(
    request: Request,
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    try:
        actividad = actividad.lower().strip()
        if actividad not in ACTIVIDADES_VALIDAS:
            return {"status": "error", "mensaje": "Actividad no válida. Debe ser 'jornada' o 'centro'."}

        # Se limita la lectura a MAX_UPLOAD_SIZE_BYTES + 1 para no cargar en
        # memoria un archivo arbitrariamente grande solo para detectarlo.
        content = await file.read(MAX_UPLOAD_SIZE_BYTES + 1)
        error_archivo = _validar_archivo_excel(file, content)
        if error_archivo:
            return {"status": "error", "mensaje": error_archivo}
        df = pd.read_excel(BytesIO(content), header=0)
        df.columns = df.columns.str.strip() 
        # Validación de columnas esperadas PASO 1 AGREGAR NUEVA COLUMNA
        columnas_necesarias = ["Id_digisalud", "HEMOGLOBINA", "GLUCOSA", "Fecha Eval. DD/MM/AAAA" , "HEMATOCRITO", "GLOBULOS BLANCOS", "PLAQUETAS"]
        for col in columnas_necesarias:
            if col not in df.columns:
                return {"status": "error", "mensaje": f"Falta columna: {col}"}

        resultado = procesar_excel_pesquisa_sanguineo(df, pais, actividad, destino_id)

        # Insertar los datos en base de datos
        conn = get_connection(pais)
        cursor = conn.cursor()

        tabla = "psi_pesquisas_x_paciente" if actividad == "jornada" else "psi_pesquisas_x_centro"
        id_campo = "jornada_id" if actividad == "jornada" else "centro_id"

        for item in resultado["pesquisas"]:
            sql = f"""
                INSERT INTO {tabla} (
                    persona_id, {id_campo}, tipo_pesquisa_id, pesq_x_pac_valor,
                    pesq_x_pac_fecha_evauacion, control_usuario_creacion, control_fecha_creacion
                )
                VALUES (%s, %s, %s, %s, %s, %s, CURDATE())
            """
            data = (
                item["persona_id"],
                item[id_campo],
                item["tipo_pesquisa_id"],
                item["pesquisa_valor"],
                item["fecha"],
                1522702145282  # Usuario fijo o reemplazable
            )
            cursor.execute(sql, data)

        conn.commit()
        conn.close()

        return {
            "status": "ok",
            "insertados": len(resultado["pesquisas"]),
            "errores": resultado["errores"]
        }

    except Exception as e:
        return _log_y_responder_error(e, "cargar_excel_pesquisa_sanguineo")


#CARGA PESQUISA SANGUINEO AVANZADA

@app.post("/api/cargar_excel_pesquisa_sanguineo_avanzada")
async def cargar_excel_pesquisa_sanguineo_avanzada(
    request: Request,
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    try:
        actividad = actividad.lower().strip()
        if actividad not in ACTIVIDADES_VALIDAS:
            return {"status": "error", "mensaje": "Actividad no válida. Debe ser 'jornada' o 'centro'."}

        # Se limita la lectura a MAX_UPLOAD_SIZE_BYTES + 1 para no cargar en
        # memoria un archivo arbitrariamente grande solo para detectarlo.
        content = await file.read(MAX_UPLOAD_SIZE_BYTES + 1)
        error_archivo = _validar_archivo_excel(file, content)
        if error_archivo:
            return {"status": "error", "mensaje": error_archivo}
        df = pd.read_excel(BytesIO(content), sheet_name="MAESTRO", header=0)
        df.columns = df.columns.str.strip()

        columnas_necesarias = ["id_digisalud", "Fecha_Ingreso"]
        for col in columnas_necesarias:
            if col not in df.columns:
                return {"status": "error", "mensaje": f"Falta columna: {col}"}

        resultado = procesar_excel_pesquisa_sanguineo_avanzada(df, pais, actividad, destino_id)

        conn = get_connection(pais)
        cursor = conn.cursor()

        tabla = "psi_pesquisas_x_paciente" if actividad == "jornada" else "psi_pesquisas_x_centro"
        id_campo = "jornada_id" if actividad == "jornada" else "centro_id"

        for item in resultado["pesquisas"]:
            sql = f"""
                INSERT INTO {tabla} (
                    persona_id, {id_campo}, tipo_pesquisa_id, pesq_x_pac_valor,
                    pesq_x_pac_fecha_evauacion, control_usuario_creacion, control_fecha_creacion
                )
                VALUES (%s, %s, %s, %s, %s, %s, CURDATE())
            """
            data = (
                item["persona_id"],
                item[id_campo],
                item["tipo_pesquisa_id"],
                item["pesquisa_valor"],
                item["fecha"],
                1522702145282  # Usuario fijo o reemplazable
            )
            cursor.execute(sql, data)

        conn.commit()
        conn.close()

        return {
            "status": "ok",
            "insertados": len(resultado["pesquisas"]),
            "errores": resultado["errores"]
        }

    except Exception as e:
        return _log_y_responder_error(e, "cargar_excel_pesquisa_sanguineo_avanzada")
#CARGA VITALES

@app.post("/api/cargar_excel_vitales")
async def cargar_excel_vitales(
    request: Request,
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    try:
        actividad = actividad.lower().strip()
        if actividad not in ACTIVIDADES_VALIDAS:
            return {"status": "error", "mensaje": "Actividad no válida. Debe ser 'jornada' o 'centro'."}

        # Se limita la lectura a MAX_UPLOAD_SIZE_BYTES + 1 para no cargar en
        # memoria un archivo arbitrariamente grande solo para detectarlo.
        content = await file.read(MAX_UPLOAD_SIZE_BYTES + 1)
        error_archivo = _validar_archivo_excel(file, content)
        if error_archivo:
            return {"status": "error", "mensaje": error_archivo}
        df = pd.read_excel(BytesIO(content), header=0)
        df.columns = df.columns.str.strip()

        columnas_necesarias = [
            "id_digisalud", "nombres", "apellidos",
            "Temperatura (°C)", "TA_SISTOLICA", "TA_DIASTOLICA",
            "Frecuencia Cardíaca (lpm)", "Frecuencia Respiratoria (rpm)",
            "Saturacion de Oxigeno", "Fecha de Evaluacion"
        ]
        for col in columnas_necesarias:
            if col not in df.columns:
                return {"status": "error", "mensaje": f"Falta columna: {col}"}

        resultado = procesar_excel_vitales(df, pais, actividad, destino_id)

        conn = get_connection(pais)
        cursor = conn.cursor()

        tabla = "psi_pesquisas_x_paciente" if actividad == "jornada" else "psi_pesquisas_x_centro"
        id_campo = "jornada_id" if actividad == "jornada" else "centro_id"

        errores = list(resultado["errores"])
        insertados = 0

        for item in resultado["pesquisas"]:
            sql = f"""
                INSERT INTO {tabla} (
                    persona_id, {id_campo}, tipo_pesquisa_id, pesq_x_pac_valor,
                    pesq_x_pac_fecha_evauacion, control_usuario_creacion, control_fecha_creacion
                )
                VALUES (%s, %s, %s, %s, %s, %s, CURDATE())
            """
            data = (
                item["persona_id"],
                item[id_campo],
                item["tipo_pesquisa_id"],
                item["pesquisa_valor"],
                item["fecha"],
                1522702145282
            )
            cursor.execute(sql, data)
            insertados += 1

        conn.commit()
        conn.close()

        return {
            "status": "ok",
            "insertados": insertados,
            "errores": errores
        }

    except Exception as e:
        return _log_y_responder_error(e, "cargar_excel_vitales")


# Rutas de descarga de archivos de plantilla
@app.get("/descargas/beneficiario", response_class=FileResponse)
def descargar_beneficiario():
    ruta = BASE_DIR / "plantillas" / "CargaMasiva_Beneficiario.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Beneficiario.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/vitales", response_class=FileResponse)
def descargar_vitales():
    ruta = BASE_DIR / "plantillas" / "plantilla_vitales_digisalud - V1P.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Vitales.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/antropometria", response_class=FileResponse)
def descargar_antropometria():
    ruta = BASE_DIR  / "plantillas" / "CargaMasiva_Pesquisa_Antropometrica.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Antropometria.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/sanguineo", response_class=FileResponse)
def descargar_sanguineo():
    ruta = BASE_DIR  / "plantillas" / "CargaMasiva_Pesquisa_Sanguineo.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Sanguineo.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/sanguineo_avanzado", response_class=FileResponse)
def descargar_sanguineo_avanzado():
    ruta = BASE_DIR  / "plantillas" / "CargaMasiva_Pesquisa_Sanguineo_Avanzada.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Sanguineo_Avanzada.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/manual", response_class=FileResponse)
def descargar_manual():
    ruta = BASE_DIR  / "plantillas" / "Manual_de_Usuario.docx"
    return FileResponse(path=ruta, filename="Manual_de_Usuario.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

 
# version 3 para usuarios mejorada
from fastapi import Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

@app.get("/usuarios/v2", response_class=HTMLResponse)
async def gestion_usuarios_v2(
    request: Request,
    pais: str = "vzla",
    organizacion_id: int = None,
    buscar: str = ""
):
    
    #   Protección de sesión
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    

    conn = get_connection(pais)
    cursor = conn.cursor()

    # Obtener organizaciones
    cursor.execute("SELECT organizacion_id, organizacion_nombre FROM psi_organizacion where organizacion_status = 0 and organizacion_id <> 5  ")
    organizaciones = [{"organizacion_id": r[0], "organizacion_nombre": r[1]} for r in cursor.fetchall()]

    # Construir base de consulta
    query = """
        SELECT u.usuario_id, u.usuario_nombre, u.usuario_apellido, u.usuario_username,
               u.usuario_status, u.usuario_organizacion_id,
               org.organizacion_nombre
        FROM psi_usuarios u
        LEFT JOIN psi_organizacion org ON u.usuario_organizacion_id = org.organizacion_id
        WHERE u.usuario_status = 0 and u.usuario_organizacion_id <> 5  
    """
    params = []

    if organizacion_id:
        query += " AND u.usuario_organizacion_id = %s"
        params.append(organizacion_id)

    if buscar:
        query += " AND (u.usuario_nombre LIKE %s OR u.usuario_apellido LIKE %s OR u.usuario_username LIKE %s)"
        like_param = f"%{buscar}%"
        params.extend([like_param, like_param, like_param])

    cursor.execute(query, params)
    usuarios = [{
        "usuario_id": r[0],
        "usuario_nombre": r[1],
        "usuario_apellido": r[2],
        "usuario_username": r[3],
        "usuario_status": r[4],
        "usuario_organizacion_id": r[5],
        "organizacion_nombre": r[6],
    } for r in cursor.fetchall()]

    conn.close()

    return templates.TemplateResponse(request, "usuarios/acciones_basev3.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "organizaciones": organizaciones,
        "usuarios": usuarios,
        "organizacion_id": organizacion_id,
        "buscar": buscar
    })


@app.post("/usuarios/v2/accion")
async def accion_usuario_v2(
    request: Request,
    accion: str = Form(...),
    pais: str = Form(...),
    usuario_id: int = Form(...),
    nuevo_correo: str = Form(None),
    nueva_contrasena: str = Form(None),
):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)

    try:
        conn = get_connection(pais)
        cursor = conn.cursor()

        if accion == "bloquear":
            cursor.execute("UPDATE psi_usuarios SET usuario_status = 22 WHERE usuario_id = %s", (usuario_id,))
        elif accion == "cambiar-contrasena":
            if not nueva_contrasena or len(nueva_contrasena) < 8:
                conn.close()
                return RedirectResponse(url=f"/usuarios/v2?pais={pais}&error=1", status_code=303)
            hashed = _hash_password_sha1(nueva_contrasena)
            cursor.execute("UPDATE psi_usuarios SET usuario_password = %s WHERE usuario_id = %s", (hashed, usuario_id))
        elif accion == "cambiar-correo":
            cursor.execute("UPDATE psi_usuarios SET usuario_username = %s WHERE usuario_id = %s", (nuevo_correo, usuario_id))

        conn.commit()
        conn.close()

        return RedirectResponse(url=f"/usuarios/v2?pais={pais}", status_code=303)

    except Exception as e:
        print("Error al ejecutar acción sobre usuario:", e)
        return RedirectResponse(url=f"/usuarios/v2?pais={pais}&error=1", status_code=303)


# para jornadas gestion
OPCIONES_POR_PAGINA_JORNADAS = (10, 20, 50, 0)  # 0 = todas


@app.get("/jornadas/v2", response_class=HTMLResponse)
async def gestion_jornadas_v2(
    request: Request,
    pais: str = "vzla",
    buscar: str = "",
    estatus: str = "",
    por_pagina: int = 10,
    pagina: int = 1
):
    # Verificación de sesión
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)

    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    estatus = int(estatus) if estatus.strip().isdigit() else None
    if por_pagina not in OPCIONES_POR_PAGINA_JORNADAS:
        por_pagina = 10
    pagina = max(pagina, 1)

    conn = get_connection(pais)
    cursor = conn.cursor()

    filtros = " FROM psi_jornadas WHERE 1 = 1"
    params = []

    if buscar:
        filtros += " AND jornada_nombre LIKE %s"
        params.append(f"%{buscar}%")

    # Los contadores por estatus ignoran el filtro de estatus, para que siempre muestren el panorama completo.
    cursor.execute("SELECT jornada_status, COUNT(*)" + filtros + " GROUP BY jornada_status", params)
    conteo_estatus = {r[0]: r[1] for r in cursor.fetchall()}

    if estatus is not None:
        filtros += " AND jornada_status = %s"
        params.append(estatus)
        total = conteo_estatus.get(estatus, 0)
    else:
        total = sum(conteo_estatus.values())

    query = "SELECT jornada_id, jornada_nombre, jornada_status, jornada_fecha_inicio" + filtros + " ORDER BY jornada_id DESC"
    if por_pagina:
        total_paginas = max((total + por_pagina - 1) // por_pagina, 1)
        pagina = min(pagina, total_paginas)
        query += " LIMIT %s OFFSET %s"
        params.extend([por_pagina, (pagina - 1) * por_pagina])
    else:
        total_paginas = 1
        pagina = 1
    cursor.execute(query, params)
    jornadas = []
    estatus_dict = {
        0: "Pendiente",
        1: "Suspendida",
        2: "En marcha",
        3: "Finalizada",
        4: "Cancelada"
    }

    for r in cursor.fetchall():
        jornadas.append({
            "jornada_id": r[0],
            "jornada_nombre": r[1],
            "jornada_status": r[2],
            "jornada_fecha_inicio": r[3],
            "jornada_status_str": estatus_dict.get(r[2], "Desconocido")
        })

    conn.close()

    return templates.TemplateResponse(request, "jornadas/jornadas_basev3.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "buscar": buscar,
        "estatus": estatus,
        "jornadas": jornadas,
        "conteo_estatus": conteo_estatus,
        "total": total,
        "por_pagina": por_pagina,
        "opciones_por_pagina": OPCIONES_POR_PAGINA_JORNADAS,
        "pagina": pagina,
        "total_paginas": total_paginas,
    })
@app.post("/jornadas/v2/accion")
async def accion_jornada_v2(
    request: Request,
    accion: str = Form(...),
    pais: str = Form(...),
    jornada_id: int = Form(...),
    nuevo_nombre: str = Form(None),
    nuevo_status: int = Form(None)
):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)

    try:
        conn = get_connection(pais)
        cursor = conn.cursor()

        if accion == "editar-nombre":
            cursor.execute(
                "UPDATE psi_jornadas SET jornada_nombre = %s WHERE jornada_id = %s",
                (nuevo_nombre, jornada_id)
            )
        elif accion == "cambiar-status":
            cursor.execute(
                "UPDATE psi_jornadas SET jornada_status = %s WHERE jornada_id = %s",
                (nuevo_status, jornada_id)
            )

        conn.commit()
        conn.close()

        return RedirectResponse(url=f"/jornadas/v2?pais={pais}", status_code=303)

    except Exception as e:
        print("Error al actualizar jornada:", e)
        return RedirectResponse(
            url=f"/jornadas/v2?pais={pais}&error=1",
            status_code=303
        )


# ---------------------------------------------------
# GESTIÓN DE BENEFICIARIOS
# Un beneficiario pertenece a las organizaciones dueñas de las jornadas o
# centros donde fue atendido (no al usuario que lo creó: la carga masiva crea
# todo con un usuario de Digisalud).
# ---------------------------------------------------
from datetime import date, datetime as _dt

PAISES_VALIDOS = {"vzla", "colombia", "elsalvador"}
ORGANIZACION_DIGISALUD_ID = 5
BENEFICIARIOS_POR_PAGINA = 50
SEXOS_VALIDOS = {"M", "F"}


def _fecha_iso(valor):
    if isinstance(valor, (date, _dt)):
        return valor.strftime("%Y-%m-%d")
    if not valor:
        return ""
    try:
        return _dt.strptime(str(valor).strip()[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _error_json(status_code: int, mensaje: str):
    return JSONResponse(status_code=status_code, content={"status": "error", "mensaje": mensaje})


@app.get("/beneficiarios/v2", response_class=HTMLResponse)
def gestion_beneficiarios_v2(
    request: Request,
    pais: str = "vzla",
    organizacion_id: int = ORGANIZACION_DIGISALUD_ID,
    buscar: str = "",
    pagina: int = 1
):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)

    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    buscar = buscar.strip()
    pagina = max(pagina, 1)

    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT organizacion_id, organizacion_nombre FROM psi_organizacion "
            "WHERE organizacion_status = 0 ORDER BY organizacion_nombre"
        )
        organizaciones = [{"organizacion_id": r[0], "organizacion_nombre": r[1]} for r in cursor.fetchall()]

        base_from = """
            FROM psi_personas p
            INNER JOIN (
                SELECT pj.persona_id FROM psi_pacientes_x_jornada pj
                INNER JOIN psi_jornadas j ON j.jornada_id = pj.jornada_id
                WHERE j.organizacion_id = %s
                UNION
                SELECT pc.persona_id FROM psi_pacientes_x_centros pc
                INNER JOIN psi_centros ce ON ce.centro_id = pc.centro_id
                WHERE ce.organizacion_id = %s
            ) org ON org.persona_id = p.persona_id
            WHERE 1 = 1
        """
        params = [organizacion_id, organizacion_id]
        if buscar:
            base_from += " AND (p.id_digisalud LIKE %s OR p.persona_nombre LIKE %s OR p.persona_apellido LIKE %s)"
            like = f"%{buscar}%"
            params.extend([like, like, like])

        cursor.execute("SELECT COUNT(*) " + base_from, params)
        total = cursor.fetchone()[0]

        total_paginas = max((total + BENEFICIARIOS_POR_PAGINA - 1) // BENEFICIARIOS_POR_PAGINA, 1)
        pagina = min(pagina, total_paginas)

        cursor.execute(
            "SELECT p.persona_id, p.id_digisalud, p.persona_nombre, p.persona_apellido, "
            "p.persona_sexo, p.persona_fecha_nacimiento "
            + base_from +
            " ORDER BY p.persona_id DESC LIMIT %s OFFSET %s",
            params + [BENEFICIARIOS_POR_PAGINA, (pagina - 1) * BENEFICIARIOS_POR_PAGINA]
        )
        beneficiarios = []
        for r in cursor.fetchall():
            fecha = _fecha_iso(r[5])
            beneficiarios.append({
                "persona_id": str(r[0]),
                "id_digisalud": r[1] or "",
                "nombres": r[2] or "",
                "apellidos": r[3] or "",
                "sexo": (r[4] or "").strip().upper(),
                "fecha_nacimiento": fecha,
                "fecha_nacimiento_str": _dt.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y") if fecha else "",
            })
    finally:
        conn.close()

    return templates.TemplateResponse(request, "beneficiarios/beneficiarios_basev3.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "cabecera": obtener_cabecera(pais),
        "organizaciones": organizaciones,
        "organizacion_id": organizacion_id,
        "buscar": buscar,
        "beneficiarios": beneficiarios,
        "total": total,
        "pagina": pagina,
        "total_paginas": total_paginas,
    })


@app.post("/beneficiarios/v2/editar")
def editar_beneficiario_v2(
    request: Request,
    pais: str = Form(...),
    persona_id: int = Form(...),
    nombres: str = Form(...),
    apellidos: str = Form(""),
    sexo: str = Form(...),
    fecha_nacimiento: str = Form(...),
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()

    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    nombres = " ".join(nombres.split()).upper()
    apellidos = " ".join(apellidos.split()).upper() or None
    sexo = sexo.strip().upper()

    if not nombres:
        return _error_json(400, "Los nombres son obligatorios.")
    if sexo not in SEXOS_VALIDOS:
        return _error_json(400, "El sexo debe ser M o F.")
    try:
        fecha = _dt.strptime(fecha_nacimiento.strip(), "%Y-%m-%d").date()
    except ValueError:
        return _error_json(400, "La fecha de nacimiento no es válida.")
    if fecha > date.today():
        return _error_json(400, "La fecha de nacimiento no puede ser futura.")
    fecha_str = fecha.strftime("%Y-%m-%d")

    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id_digisalud, persona_nombre, persona_apellido, persona_sexo, persona_fecha_nacimiento, "
            "persona_cedula, persona_cedula_escolar FROM psi_personas WHERE persona_id = %s",
            (persona_id,)
        )
        actual = cursor.fetchone()
        if not actual:
            return _error_json(404, "El beneficiario no existe.")

        id_anterior, nom_ant, ape_ant, sexo_ant, fecha_ant, cedula, cedula_escolar = actual
        sin_cambios = (
            (nom_ant or "").strip().upper() == nombres
            and ((ape_ant or "").strip().upper() or None) == apellidos
            and (sexo_ant or "").strip().upper() == sexo
            and _fecha_iso(fecha_ant) == fecha_str
        )
        if sin_cambios:
            return {"status": "ok", "mensaje": "No hubo cambios.", "id_digisalud": id_anterior}

        nuevo_id = generar_id_digisalud(nombres, apellidos, sexo, obtener_cabecera(pais), fecha_str)

        # La carga masiva usa el id_digisalud como cédula de respaldo; se mantienen sincronizadas.
        nueva_cedula = nuevo_id if cedula == id_anterior else cedula
        nueva_cedula_escolar = nuevo_id if cedula_escolar == id_anterior else cedula_escolar

        if nuevo_id != id_anterior:
            cursor.execute(
                "SELECT persona_nombre, persona_apellido FROM psi_personas "
                "WHERE persona_id <> %s AND (id_digisalud = %s OR persona_cedula = %s OR persona_cedula_escolar = %s) "
                "LIMIT 1",
                (persona_id, nuevo_id, nuevo_id, nuevo_id)
            )
            duplicado = cursor.fetchone()
            if duplicado:
                nombre_dup = " ".join(x for x in duplicado if x)
                return _error_json(
                    409,
                    f"El ID DIGISALUD {nuevo_id} ya existe y pertenece a otro beneficiario ({nombre_dup}). "
                    "No se guardaron los cambios."
                )

        cursor.execute(
            "UPDATE psi_personas SET persona_nombre = %s, persona_apellido = %s, persona_sexo = %s, "
            "persona_fecha_nacimiento = %s, id_digisalud = %s, persona_cedula = %s, persona_cedula_escolar = %s "
            "WHERE persona_id = %s",
            (nombres, apellidos, sexo, fecha_str, nuevo_id, nueva_cedula, nueva_cedula_escolar, persona_id)
        )
        conn.commit()
        logger.info("Beneficiario actualizado persona_id=%s", persona_id)
        return {"status": "ok", "mensaje": "Beneficiario actualizado.", "id_digisalud": nuevo_id}

    except IntegrityError as e:
        conn.rollback()
        if es_error_duplicado_mysql(e):
            return _error_json(409, "El ID DIGISALUD generado ya existe para otro beneficiario. No se guardaron los cambios.")
        logger.exception("Error de integridad al editar beneficiario persona_id=%s", persona_id)
        return _error_json(500, "No se pudo actualizar el beneficiario.")
    except Exception:
        conn.rollback()
        logger.exception("Error al editar beneficiario persona_id=%s", persona_id)
        return _error_json(500, "No se pudo actualizar el beneficiario.")
    finally:
        conn.close()


# ---------------------------------------------------
# COPIAR BENEFICIARIOS ENTRE JORNADAS
# Copia la inscripción (psi_pacientes_x_jornada), las autorizaciones y la
# escolaridad de los beneficiarios elegidos de una jornada origen a una destino.
# ---------------------------------------------------
from pydantic import BaseModel

MAX_BENEFICIARIOS_COPIA = 5000


def _user_id_sesion(request: Request):
    sesion = SESSIONS.get(request.cookies.get("session") or "")
    return sesion["user_id"] if sesion else None


def _placeholders(n: int) -> str:
    return ", ".join(["%s"] * n)


def _institucion_de_jornada(cursor, jornada_id: int):
    cursor.execute("SELECT institucion_id FROM psi_instituciones_x_jornada WHERE jornada_id = %s", (jornada_id,))
    filas = cursor.fetchall()
    return filas[0][0] if len(filas) == 1 else None


@app.get("/jornadas/v2/copiar", response_class=HTMLResponse)
def copiar_beneficiarios_jornadas(request: Request, pais: str = "vzla"):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if pais not in PAISES_VALIDOS:
        pais = "vzla"

    estatus_dict = {0: "Pendiente", 1: "Suspendida", 2: "En marcha", 3: "Finalizada", 4: "Cancelada"}
    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT j.jornada_id, j.jornada_nombre, j.jornada_status, COALESCE(pj.total, 0)
            FROM psi_jornadas j
            LEFT JOIN (
                SELECT jornada_id, COUNT(*) AS total FROM psi_pacientes_x_jornada GROUP BY jornada_id
            ) pj ON pj.jornada_id = j.jornada_id
            ORDER BY j.jornada_id DESC
        """)
        jornadas = [{
            "jornada_id": r[0],
            "jornada_nombre": r[1] or "",
            "estatus": estatus_dict.get(r[2], "Desconocido"),
            "total": r[3],
        } for r in cursor.fetchall()]
    finally:
        conn.close()

    return templates.TemplateResponse(request, "jornadas/copiar_beneficiarios.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "jornadas": jornadas,
    })


@app.get("/api/jornadas/beneficiarios")
def api_beneficiarios_jornada(request: Request, pais: str, origen_id: int, destino_id: int = 0):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.persona_id, p.id_digisalud, p.persona_nombre, p.persona_apellido, p.persona_sexo,
                   p.persona_fecha_nacimiento, e.escolaridad_escuela, e.escolaridad_grado, e.escolaridad_seccion,
                   (d.persona_id IS NOT NULL) AS ya_en_destino
            FROM psi_pacientes_x_jornada s
            INNER JOIN psi_personas p ON p.persona_id = s.persona_id
            LEFT JOIN psi_escolaridad e ON e.persona_id = s.persona_id AND e.jornada_id = s.jornada_id
            LEFT JOIN psi_pacientes_x_jornada d ON d.persona_id = s.persona_id AND d.jornada_id = %s
            WHERE s.jornada_id = %s
            ORDER BY p.persona_apellido, p.persona_nombre
        """, (destino_id, origen_id))
        beneficiarios = []
        for r in cursor.fetchall():
            fecha = _fecha_iso(r[5])
            beneficiarios.append({
                "persona_id": str(r[0]),
                "id_digisalud": r[1] or "",
                "nombres": r[2] or "",
                "apellidos": r[3] or "",
                "sexo": (r[4] or "").strip().upper(),
                "fecha_nacimiento": _dt.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y") if fecha else "",
                "escuela": r[6] or "",
                "grado": " ".join(x for x in (r[7], r[8]) if x),
                "ya_en_destino": bool(r[9]),
            })
    finally:
        conn.close()
    return {"status": "ok", "beneficiarios": beneficiarios}


class CopiaBeneficiariosIn(BaseModel):
    pais: str
    origen_id: int
    destino_id: int
    persona_ids: list[int]


@app.post("/api/jornadas/copiar_beneficiarios")
def api_copiar_beneficiarios(request: Request, datos: CopiaBeneficiariosIn):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if datos.pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")
    if datos.origen_id == datos.destino_id:
        return _error_json(400, "La jornada origen y la de destino deben ser distintas.")
    persona_ids = sorted(set(datos.persona_ids))
    if not persona_ids:
        return _error_json(400, "Selecciona al menos un beneficiario.")
    if len(persona_ids) > MAX_BENEFICIARIOS_COPIA:
        return _error_json(400, f"No se pueden copiar más de {MAX_BENEFICIARIOS_COPIA} beneficiarios a la vez.")

    user_id = _user_id_sesion(request)
    conn = get_connection(datos.pais)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT jornada_id FROM psi_jornadas WHERE jornada_id IN (%s, %s)",
            (datos.origen_id, datos.destino_id)
        )
        if len(cursor.fetchall()) != 2:
            return _error_json(404, "La jornada origen o la de destino no existe.")

        institucion_destino = _institucion_de_jornada(cursor, datos.destino_id)
        if institucion_destino is None:
            return _error_json(400, "La jornada destino no tiene una institución asociada (o tiene más de una).")

        ph = _placeholders(len(persona_ids))

        # Solo se copian personas que realmente están en la jornada origen.
        cursor.execute(
            f"SELECT persona_id, persona_id_responsable FROM psi_pacientes_x_jornada "
            f"WHERE jornada_id = %s AND persona_id IN ({ph})",
            [datos.origen_id] + persona_ids
        )
        en_origen = {r[0]: r[1] for r in cursor.fetchall()}

        cursor.execute(
            f"SELECT persona_id FROM psi_pacientes_x_jornada WHERE jornada_id = %s AND persona_id IN ({ph})",
            [datos.destino_id] + persona_ids
        )
        ya_en_destino = {r[0] for r in cursor.fetchall()}

        a_copiar = [pid for pid in persona_ids if pid in en_origen and pid not in ya_en_destino]
        no_en_origen = len([pid for pid in persona_ids if pid not in en_origen])

        if not a_copiar:
            return {
                "status": "ok",
                "mensaje": "No había beneficiarios nuevos para copiar.",
                "copiados": 0, "autorizaciones": 0, "escolaridades": 0,
                "ya_en_destino": len(ya_en_destino), "no_en_origen": no_en_origen,
            }

        ph_copiar = _placeholders(len(a_copiar))
        ahora = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.executemany(
            "INSERT INTO psi_pacientes_x_jornada (persona_id, jornada_id, institucion_id, pac_x_jornada_status, "
            "persona_id_responsable, control_fecha_creacion, control_usuario_creacion) "
            "VALUES (%s, %s, %s, 0, %s, %s, %s)",
            [(pid, datos.destino_id, institucion_destino, en_origen[pid], ahora, user_id) for pid in a_copiar]
        )

        cursor.execute(
            f"SELECT persona_id, autorizacion_id FROM psi_aut_pac_x_jornada "
            f"WHERE jornada_id = %s AND persona_id IN ({ph_copiar})",
            [datos.origen_id] + a_copiar
        )
        autorizaciones = [(r[0], datos.destino_id, r[1]) for r in cursor.fetchall()]
        if autorizaciones:
            cursor.executemany(
                "INSERT INTO psi_aut_pac_x_jornada (persona_id, jornada_id, autorizacion_id) VALUES (%s, %s, %s)",
                autorizaciones
            )

        cursor.execute(
            f"SELECT persona_id, escolaridad_grado, escolaridad_seccion, escolaridad_turno, escolaridad_escuela "
            f"FROM psi_escolaridad WHERE jornada_id = %s AND persona_id IN ({ph_copiar})",
            [datos.origen_id] + a_copiar
        )
        escolaridades = [(r[0], datos.destino_id, r[1], r[2], r[3], r[4]) for r in cursor.fetchall()]
        if escolaridades:
            cursor.executemany(
                "INSERT INTO psi_escolaridad (persona_id, jornada_id, escolaridad_grado, escolaridad_seccion, "
                "escolaridad_turno, escolaridad_escuela) VALUES (%s, %s, %s, %s, %s, %s)",
                escolaridades
            )

        conn.commit()
        logger.info(
            "Copia de beneficiarios jornada %s -> %s: %s pacientes, %s autorizaciones, %s escolaridades (usuario_id=%s)",
            datos.origen_id, datos.destino_id, len(a_copiar), len(autorizaciones), len(escolaridades), user_id
        )
        return {
            "status": "ok",
            "mensaje": "Se copió 1 beneficiario." if len(a_copiar) == 1 else f"Se copiaron {len(a_copiar)} beneficiarios.",
            "copiados": len(a_copiar),
            "autorizaciones": len(autorizaciones),
            "escolaridades": len(escolaridades),
            "ya_en_destino": len(ya_en_destino),
            "no_en_origen": no_en_origen,
        }

    except IntegrityError as e:
        conn.rollback()
        if es_error_duplicado_mysql(e):
            return _error_json(409, "Algún beneficiario fue agregado a la jornada destino mientras copiabas. Recarga la lista e intenta de nuevo.")
        logger.exception("Error de integridad copiando beneficiarios %s -> %s", datos.origen_id, datos.destino_id)
        return _error_json(500, "No se pudieron copiar los beneficiarios. No se guardó ningún cambio.")
    except Exception:
        conn.rollback()
        logger.exception("Error copiando beneficiarios %s -> %s", datos.origen_id, datos.destino_id)
        return _error_json(500, "No se pudieron copiar los beneficiarios. No se guardó ningún cambio.")
    finally:
        conn.close()



# ---------------------------------------------------
# GESTIÓN DE CENTROS
# Por ahora solo permite consultar centros y editar su nombre.
# ---------------------------------------------------
OPCIONES_POR_PAGINA_CENTROS = (10, 20, 50, 0)  # 0 = todos
NOMBRE_CENTRO_MAX = 255


@app.get("/centros/v2", response_class=HTMLResponse)
def gestion_centros_v2(
    request: Request,
    pais: str = "vzla",
    buscar: str = "",
    por_pagina: int = 10,
    pagina: int = 1
):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)

    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    buscar = buscar.strip()
    if por_pagina not in OPCIONES_POR_PAGINA_CENTROS:
        por_pagina = 10
    pagina = max(pagina, 1)

    filtros = " FROM psi_centros c WHERE 1 = 1"
    params = []
    if buscar:
        filtros += " AND (c.centro_nombre LIKE %s OR c.centro_id LIKE %s)"
        params.extend([f"%{buscar}%", f"%{buscar}%"])

    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*)" + filtros, params)
        total = cursor.fetchone()[0]

        query = (
            "SELECT c.centro_id, c.centro_nombre, "
            "(SELECT COUNT(*) FROM psi_pacientes_x_centros pc WHERE pc.centro_id = c.centro_id)"
            + filtros + " ORDER BY c.centro_id DESC"
        )
        if por_pagina:
            total_paginas = max((total + por_pagina - 1) // por_pagina, 1)
            pagina = min(pagina, total_paginas)
            query += " LIMIT %s OFFSET %s"
            params.extend([por_pagina, (pagina - 1) * por_pagina])
        else:
            total_paginas = 1
            pagina = 1
        cursor.execute(query, params)
        centros = [{
            "centro_id": r[0],
            "centro_nombre": r[1] or "",
            "total_beneficiarios": r[2],
        } for r in cursor.fetchall()]
    finally:
        conn.close()

    return templates.TemplateResponse(request, "centros/centros_basev3.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "buscar": buscar,
        "centros": centros,
        "total": total,
        "por_pagina": por_pagina,
        "opciones_por_pagina": OPCIONES_POR_PAGINA_CENTROS,
        "pagina": pagina,
        "total_paginas": total_paginas,
    })


@app.post("/centros/v2/accion")
def accion_centro_v2(
    request: Request,
    accion: str = Form(...),
    pais: str = Form(...),
    centro_id: int = Form(...),
    nuevo_nombre: str = Form(""),
):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")
    if accion != "editar-nombre":
        return _error_json(400, "Acción no válida.")

    nuevo_nombre = " ".join(nuevo_nombre.split())
    if not nuevo_nombre:
        return _error_json(400, "El nombre del centro es obligatorio.")
    if len(nuevo_nombre) > NOMBRE_CENTRO_MAX:
        return _error_json(400, f"El nombre no puede superar {NOMBRE_CENTRO_MAX} caracteres.")

    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT centro_nombre FROM psi_centros WHERE centro_id = %s", (centro_id,))
        actual = cursor.fetchone()
        if not actual:
            return _error_json(404, "El centro no existe.")
        if (actual[0] or "") == nuevo_nombre:
            return {"status": "ok", "mensaje": "No hubo cambios.", "centro_nombre": nuevo_nombre}

        cursor.execute(
            "UPDATE psi_centros SET centro_nombre = %s WHERE centro_id = %s",
            (nuevo_nombre, centro_id)
        )
        conn.commit()
        logger.info("Centro %s renombrado (usuario_id=%s)", centro_id, _user_id_sesion(request))
        return {"status": "ok", "mensaje": "Nombre del centro actualizado.", "centro_nombre": nuevo_nombre}
    except Exception:
        conn.rollback()
        logger.exception("Error al renombrar centro_id=%s", centro_id)
        return _error_json(500, "No se pudo actualizar el centro.")
    finally:
        conn.close()


# ---------------------------------------------------
# COPIAR BENEFICIARIOS DE UNA JORNADA A UN CENTRO (J-C)
# Mismas reglas que J-J: se copia la inscripción, las autorizaciones y la
# escolaridad de los beneficiarios elegidos; quienes ya están en el centro
# destino se omiten; todo en una sola transacción.
# ---------------------------------------------------
def _institucion_de_centro(cursor, centro_id: int):
    cursor.execute("SELECT institucion_id FROM psi_instituciones_x_centro WHERE centro_id = %s", (centro_id,))
    filas = cursor.fetchall()
    return filas[0][0] if len(filas) == 1 else None


def _tabla_tiene_columna(cursor, tabla: str, columna: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s LIMIT 1",
        (tabla, columna)
    )
    return cursor.fetchone() is not None


@app.get("/centros/v2/copiar", response_class=HTMLResponse)
def copiar_beneficiarios_jornada_centro(request: Request, pais: str = "vzla"):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if pais not in PAISES_VALIDOS:
        pais = "vzla"

    estatus_dict = {0: "Pendiente", 1: "Suspendida", 2: "En marcha", 3: "Finalizada", 4: "Cancelada"}
    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT j.jornada_id, j.jornada_nombre, j.jornada_status, COALESCE(pj.total, 0)
            FROM psi_jornadas j
            LEFT JOIN (
                SELECT jornada_id, COUNT(*) AS total FROM psi_pacientes_x_jornada GROUP BY jornada_id
            ) pj ON pj.jornada_id = j.jornada_id
            ORDER BY j.jornada_id DESC
        """)
        jornadas = [{
            "jornada_id": r[0],
            "jornada_nombre": r[1] or "",
            "estatus": estatus_dict.get(r[2], "Desconocido"),
            "total": r[3],
        } for r in cursor.fetchall()]

        cursor.execute("""
            SELECT c.centro_id, c.centro_nombre, COALESCE(pc.total, 0)
            FROM psi_centros c
            LEFT JOIN (
                SELECT centro_id, COUNT(*) AS total FROM psi_pacientes_x_centros GROUP BY centro_id
            ) pc ON pc.centro_id = c.centro_id
            ORDER BY c.centro_id DESC
        """)
        centros = [{
            "centro_id": r[0],
            "centro_nombre": r[1] or "",
            "estatus": "",
            "total": r[2],
        } for r in cursor.fetchall()]
    finally:
        conn.close()

    return templates.TemplateResponse(request, "jornadas/copiar_beneficiarios.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "modo": "jc",
        "origenes": jornadas,
        "destinos": centros,
    })


@app.get("/api/jornada_centro/beneficiarios")
def api_beneficiarios_jornada_para_centro(request: Request, pais: str, origen_id: int, destino_id: int = 0):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    conn = get_connection(pais)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.persona_id, p.id_digisalud, p.persona_nombre, p.persona_apellido, p.persona_sexo,
                   p.persona_fecha_nacimiento, e.escolaridad_escuela, e.escolaridad_grado, e.escolaridad_seccion,
                   (d.persona_id IS NOT NULL) AS ya_en_destino
            FROM psi_pacientes_x_jornada s
            INNER JOIN psi_personas p ON p.persona_id = s.persona_id
            LEFT JOIN psi_escolaridad e ON e.persona_id = s.persona_id AND e.jornada_id = s.jornada_id
            LEFT JOIN psi_pacientes_x_centros d ON d.persona_id = s.persona_id AND d.centro_id = %s
            WHERE s.jornada_id = %s
            ORDER BY p.persona_apellido, p.persona_nombre
        """, (destino_id, origen_id))
        beneficiarios = []
        for r in cursor.fetchall():
            fecha = _fecha_iso(r[5])
            beneficiarios.append({
                "persona_id": str(r[0]),
                "id_digisalud": r[1] or "",
                "nombres": r[2] or "",
                "apellidos": r[3] or "",
                "sexo": (r[4] or "").strip().upper(),
                "fecha_nacimiento": _dt.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y") if fecha else "",
                "escuela": r[6] or "",
                "grado": " ".join(x for x in (r[7], r[8]) if x),
                "ya_en_destino": bool(r[9]),
            })
    finally:
        conn.close()
    return {"status": "ok", "beneficiarios": beneficiarios}


class CopiaJornadaCentroIn(BaseModel):
    pais: str
    origen_id: int   # jornada
    destino_id: int  # centro
    persona_ids: list[int]


@app.post("/api/jornada_centro/copiar_beneficiarios")
def api_copiar_beneficiarios_jornada_centro(request: Request, datos: CopiaJornadaCentroIn):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if datos.pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")
    persona_ids = sorted(set(datos.persona_ids))
    if not persona_ids:
        return _error_json(400, "Selecciona al menos un beneficiario.")
    if len(persona_ids) > MAX_BENEFICIARIOS_COPIA:
        return _error_json(400, f"No se pueden copiar más de {MAX_BENEFICIARIOS_COPIA} beneficiarios a la vez.")

    user_id = _user_id_sesion(request)
    conn = get_connection(datos.pais)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM psi_jornadas WHERE jornada_id = %s", (datos.origen_id,))
        if not cursor.fetchone():
            return _error_json(404, "La jornada origen no existe.")
        cursor.execute("SELECT 1 FROM psi_centros WHERE centro_id = %s", (datos.destino_id,))
        if not cursor.fetchone():
            return _error_json(404, "El centro destino no existe.")

        institucion_destino = _institucion_de_centro(cursor, datos.destino_id)
        if institucion_destino is None:
            return _error_json(400, "El centro destino no tiene una institución asociada (o tiene más de una).")

        ph = _placeholders(len(persona_ids))

        # Solo se copian personas que realmente están en la jornada origen.
        cursor.execute(
            f"SELECT persona_id, persona_id_responsable FROM psi_pacientes_x_jornada "
            f"WHERE jornada_id = %s AND persona_id IN ({ph})",
            [datos.origen_id] + persona_ids
        )
        en_origen = {r[0]: r[1] for r in cursor.fetchall()}

        cursor.execute(
            f"SELECT persona_id FROM psi_pacientes_x_centros WHERE centro_id = %s AND persona_id IN ({ph})",
            [datos.destino_id] + persona_ids
        )
        ya_en_destino = {r[0] for r in cursor.fetchall()}

        a_copiar = [pid for pid in persona_ids if pid in en_origen and pid not in ya_en_destino]
        no_en_origen = len([pid for pid in persona_ids if pid not in en_origen])

        if not a_copiar:
            return {
                "status": "ok",
                "mensaje": "No había beneficiarios nuevos para copiar.",
                "copiados": 0, "autorizaciones": 0, "escolaridades": 0,
                "ya_en_destino": len(ya_en_destino), "no_en_origen": no_en_origen,
            }

        ph_copiar = _placeholders(len(a_copiar))
        ahora = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        # persona_id_responsable puede no existir en la tabla de centros.
        if _tabla_tiene_columna(cursor, "psi_pacientes_x_centros", "persona_id_responsable"):
            cursor.executemany(
                "INSERT INTO psi_pacientes_x_centros (persona_id, centro_id, institucion_id, pac_x_centro_status, "
                "persona_id_responsable, control_fecha_creacion, control_usuario_creacion) "
                "VALUES (%s, %s, %s, 0, %s, %s, %s)",
                [(pid, datos.destino_id, institucion_destino, en_origen[pid], ahora, user_id) for pid in a_copiar]
            )
        else:
            cursor.executemany(
                "INSERT INTO psi_pacientes_x_centros (persona_id, centro_id, institucion_id, pac_x_centro_status, "
                "control_fecha_creacion, control_usuario_creacion) "
                "VALUES (%s, %s, %s, 0, %s, %s)",
                [(pid, datos.destino_id, institucion_destino, ahora, user_id) for pid in a_copiar]
            )

        cursor.execute(
            f"SELECT persona_id, autorizacion_id FROM psi_aut_pac_x_jornada "
            f"WHERE jornada_id = %s AND persona_id IN ({ph_copiar})",
            [datos.origen_id] + a_copiar
        )
        autorizaciones = [(r[0], datos.destino_id, r[1]) for r in cursor.fetchall()]
        if autorizaciones:
            cursor.executemany(
                "INSERT INTO psi_aut_pac_x_centro (persona_id, centro_id, autorizacion_id) VALUES (%s, %s, %s)",
                autorizaciones
            )

        cursor.execute(
            f"SELECT persona_id, escolaridad_grado, escolaridad_seccion, escolaridad_turno, escolaridad_escuela "
            f"FROM psi_escolaridad WHERE jornada_id = %s AND persona_id IN ({ph_copiar})",
            [datos.origen_id] + a_copiar
        )
        escolaridades = [(r[0], datos.destino_id, r[1], r[2], r[3], r[4]) for r in cursor.fetchall()]
        if escolaridades:
            cursor.executemany(
                "INSERT INTO psi_escolaridad_centro (persona_id, centro_id, escolaridad_grado, escolaridad_seccion, "
                "escolaridad_turno, escolaridad_escuela) VALUES (%s, %s, %s, %s, %s, %s)",
                escolaridades
            )

        conn.commit()
        logger.info(
            "Copia de beneficiarios jornada %s -> centro %s: %s pacientes, %s autorizaciones, %s escolaridades (usuario_id=%s)",
            datos.origen_id, datos.destino_id, len(a_copiar), len(autorizaciones), len(escolaridades), user_id
        )
        return {
            "status": "ok",
            "mensaje": "Se copió 1 beneficiario." if len(a_copiar) == 1 else f"Se copiaron {len(a_copiar)} beneficiarios.",
            "copiados": len(a_copiar),
            "autorizaciones": len(autorizaciones),
            "escolaridades": len(escolaridades),
            "ya_en_destino": len(ya_en_destino),
            "no_en_origen": no_en_origen,
        }

    except IntegrityError as e:
        conn.rollback()
        if es_error_duplicado_mysql(e):
            return _error_json(409, "Algún beneficiario fue agregado al centro destino mientras copiabas. Recarga la lista e intenta de nuevo.")
        logger.exception("Error de integridad copiando beneficiarios jornada %s -> centro %s", datos.origen_id, datos.destino_id)
        return _error_json(500, "No se pudieron copiar los beneficiarios. No se guardó ningún cambio.")
    except Exception:
        conn.rollback()
        logger.exception("Error copiando beneficiarios jornada %s -> centro %s", datos.origen_id, datos.destino_id)
        return _error_json(500, "No se pudieron copiar los beneficiarios. No se guardó ningún cambio.")
    finally:
        conn.close()


# ---------------------------------------------------
# UNIFICAR BENEFICIARIOS DUPLICADOS
# Reemplaza el proceso manual con los procedimientos a001..a010: el usuario busca,
# compara, elige cuál registro se queda y revisa una vista previa antes de unificar.
# La lógica vive en backend/services/fusion_beneficiarios.py.
# ---------------------------------------------------
from backend.services import fusion_beneficiarios as fusion


@app.get("/beneficiarios/v2/duplicados", response_class=HTMLResponse)
def unificar_duplicados(request: Request, pais: str = "vzla"):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    return templates.TemplateResponse(request, "beneficiarios/duplicados.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
    })


@app.get("/api/duplicados/buscar")
def api_duplicados_buscar(request: Request, pais: str, nombre: str = "", apellido: str = "", documento: str = ""):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    conn = get_connection(pais)
    try:
        datos = fusion.buscar_personas(conn.cursor(), nombre, apellido, documento)
        return {"status": "ok", **datos}
    except fusion.FusionError as e:
        return _error_json(400, str(e))
    except Exception:
        logger.exception("Error buscando duplicados")
        return _error_json(500, "No se pudo realizar la búsqueda.")
    finally:
        conn.close()


@app.get("/api/duplicados/comparar")
def api_duplicados_comparar(request: Request, pais: str, id_a: int, id_b: int):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    conn = get_connection(pais)
    try:
        return {"status": "ok", **fusion.comparar(conn.cursor(), id_a, id_b)}
    except fusion.FusionError as e:
        return _error_json(400, str(e))
    except Exception:
        logger.exception("Error comparando beneficiarios %s y %s", id_a, id_b)
        return _error_json(500, "No se pudo cargar la comparación.")
    finally:
        conn.close()


@app.get("/api/duplicados/plan")
def api_duplicados_plan(request: Request, pais: str, queda_id: int, fuera_id: int):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    conn = get_connection(pais)
    try:
        return {"status": "ok", "plan": fusion.construir_plan(conn.cursor(), queda_id, fuera_id)}
    except fusion.FusionError as e:
        return _error_json(400, str(e))
    except Exception:
        logger.exception("Error armando plan de unificación %s <- %s", queda_id, fuera_id)
        return _error_json(500, "No se pudo preparar la vista previa.")
    finally:
        conn.close()


class FusionIn(BaseModel):
    pais: str
    queda_id: int
    fuera_id: int
    # Debe ser True cuando hay advertencias de nivel "alto" (sexo/fecha distinta, familiares, etc.).
    confirmar_advertencias: bool = False


@app.post("/api/duplicados/unificar")
def api_duplicados_unificar(request: Request, datos: FusionIn):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if datos.pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    user_id = _user_id_sesion(request)
    conn = get_connection(datos.pais)
    try:
        cursor = conn.cursor()
        # Misma validación que la vista previa, pero con las filas bloqueadas, para no confiar en lo que mostró el navegador.
        plan = fusion.construir_plan(cursor, datos.queda_id, datos.fuera_id, bloquear=True)
        if any(a["nivel"] == "alto" for a in plan["advertencias"]) and not datos.confirmar_advertencias:
            conn.rollback()
            return _error_json(409, "Hay diferencias entre los registros. Confirma que deseas unificarlos de todos modos.")

        resultado = fusion.ejecutar_fusion(cursor, datos.queda_id, datos.fuera_id, user_id)
        conn.commit()
        logger.info(
            "Beneficiarios unificados en %s: queda=%s eliminado=%s %s (usuario_id=%s)",
            datos.pais, datos.queda_id, datos.fuera_id, resultado["contadores"], user_id
        )
        return {
            "status": "ok",
            "mensaje": "Beneficiarios unificados correctamente.",
            "queda": resultado["plan"]["queda"],
            "fuera": resultado["plan"]["fuera"],
            "contadores": resultado["contadores"],
        }

    except fusion.FusionError as e:
        conn.rollback()
        return _error_json(400, str(e))
    except Exception:
        conn.rollback()
        logger.exception("Error unificando beneficiarios queda=%s fuera=%s", datos.queda_id, datos.fuera_id)
        return _error_json(500, "No se pudo unificar. No se guardó ningún cambio.")
    finally:
        conn.close()


# ---------------------------------------------------
# POSIBLES DUPLICADOS (detección automática)
# Lista pares con mismo apellido, sexo y fecha de nacimiento y nombres iguales o
# contenidos; cada par enlaza con la pantalla de unificación ya comparado.
# ---------------------------------------------------
@app.get("/beneficiarios/v2/posibles-duplicados", response_class=HTMLResponse)
def posibles_duplicados(request: Request, pais: str = "vzla"):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    return templates.TemplateResponse(request, "beneficiarios/posibles_duplicados.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
    })


@app.get("/api/duplicados/posibles")
def api_duplicados_posibles(request: Request, pais: str):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    conn = get_connection(pais)
    try:
        return {"status": "ok", **fusion.listar_posibles_duplicados(conn.cursor())}
    except Exception:
        logger.exception("Error listando posibles duplicados")
        return _error_json(500, "No se pudo generar la lista de posibles duplicados.")
    finally:
        conn.close()


# ---------------------------------------------------
# EVALUACIONES DUPLICADAS EN CENTROS
# Detecta mediciones repetidas (misma persona, centro, tipo de pesquisa y fecha) por recargas del
# Excel y permite eliminar las sobrantes. Solo aplica a centros: en jornadas no se permite evaluar
# dos veces a la misma persona. La lógica vive en backend/services/evaluaciones_duplicadas.py.
# ---------------------------------------------------
from backend.services import evaluaciones_duplicadas as evdup


@app.get("/centros/v2/evaluaciones-duplicadas", response_class=HTMLResponse)
def evaluaciones_duplicadas(request: Request, pais: str = "vzla"):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    return templates.TemplateResponse(request, "centros/evaluaciones_duplicadas.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
    })


def _evdup_lectura(request: Request, pais: str, funcion, contexto: str):
    """Ejecuta una consulta de solo lectura del servicio con sesión, país y manejo de errores comunes."""
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")
    conn = get_connection(pais)
    try:
        return {"status": "ok", **funcion(conn.cursor())}
    except evdup.EvaluacionesError as e:
        return _error_json(400, str(e))
    except Exception:
        logger.exception("Error en evaluaciones duplicadas (%s)", contexto)
        return _error_json(500, "No se pudo completar la consulta.")
    finally:
        conn.close()


@app.get("/api/eval_dup/clientes")
def api_evdup_clientes(request: Request, pais: str):
    return _evdup_lectura(request, pais, lambda cur: {"clientes": evdup.listar_clientes(cur)}, "clientes")


@app.get("/api/eval_dup/centros")
def api_evdup_centros(request: Request, pais: str, organizacion_id: int):
    return _evdup_lectura(request, pais, lambda cur: {"centros": evdup.listar_centros(cur, organizacion_id)}, "centros")


@app.get("/api/eval_dup/beneficiarios")
def api_evdup_beneficiarios(request: Request, pais: str, centro_id: int, todos: bool = False):
    return _evdup_lectura(request, pais, lambda cur: evdup.beneficiarios_del_centro(cur, centro_id, todos), "beneficiarios")


@app.get("/api/eval_dup/detalle")
def api_evdup_detalle(request: Request, pais: str, centro_id: int, persona_id: int):
    return _evdup_lectura(request, pais, lambda cur: evdup.detalle_beneficiario(cur, centro_id, persona_id), "detalle")


class EvalDupIn(BaseModel):
    pais: str
    centro_id: int
    modo: str                                   # 'seleccion' (un beneficiario) o 'exactas' (solo copias idénticas)
    persona_id: int | None = None
    conservar: dict[str, int] = {}              # {"AAAA-MM-DD|PESQUISA": nº de carga que se conserva}
    esperado: int | None = None                 # total que vio el usuario en la vista previa


@app.post("/api/eval_dup/plan")
def api_evdup_plan(request: Request, datos: EvalDupIn):
    def calcular(cur):
        plan = evdup.construir_plan(cur, datos.centro_id, datos.modo, datos.persona_id, datos.conservar)
        return {"total": plan["total"], "beneficiarios": plan["beneficiarios"]}
    return _evdup_lectura(request, datos.pais, calcular, "plan")


@app.post("/api/eval_dup/eliminar")
def api_evdup_eliminar(request: Request, datos: EvalDupIn):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if datos.pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    user_id = _user_id_sesion(request)
    conn = get_connection(datos.pais)
    try:
        resultado = evdup.eliminar_repetidas(
            conn.cursor(), datos.centro_id, datos.modo, datos.persona_id, datos.conservar, datos.esperado
        )
        conn.commit()
        logger.info(
            "Evaluaciones repetidas eliminadas en %s centro=%s modo=%s persona=%s: %s filas, %s beneficiarios (usuario_id=%s)",
            datos.pais, datos.centro_id, datos.modo, datos.persona_id, resultado["eliminadas"], resultado["beneficiarios"], user_id
        )
        return {"status": "ok", "mensaje": "Evaluaciones repetidas eliminadas.", **resultado}
    except evdup.EvaluacionesError as e:
        conn.rollback()
        return _error_json(409, str(e))
    except Exception:
        conn.rollback()
        logger.exception("Error eliminando evaluaciones repetidas centro=%s", datos.centro_id)
        return _error_json(500, "No se pudieron eliminar las evaluaciones. No se guardó ningún cambio.")
    finally:
        conn.close()


# ---------------------------------------------------
# BENEFICIARIOS HUÉRFANOS
# Personas que nunca fueron evaluadas, no están en ninguna jornada ni centro y no tienen relación
# familiar con otro beneficiario. Se listan con su fecha de registro para decidir si se eliminan.
# La lógica vive en backend/services/beneficiarios_huerfanos.py.
# ---------------------------------------------------
from backend.services import beneficiarios_huerfanos as huerfanos


@app.get("/beneficiarios/v2/huerfanos", response_class=HTMLResponse)
def beneficiarios_huerfanos(request: Request, pais: str = "vzla"):
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if pais not in PAISES_VALIDOS:
        pais = "vzla"
    return templates.TemplateResponse(request, "beneficiarios/huerfanos.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
    })


@app.get("/api/huerfanos")
def api_huerfanos_listar(request: Request, pais: str, refrescar: bool = False):
    """Devuelve todos los huérfanos de una vez; la búsqueda, el rango de fechas, el orden y la paginación
    los hace el navegador. La búsqueda recorre a todas las personas (≈5 s en Venezuela), así que el resultado
    se guarda en memoria (ver `listar_con_cache`); `refrescar=true` fuerza el recálculo."""
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    try:
        return {"status": "ok", **huerfanos.listar_con_cache(pais, get_connection, refrescar)}
    except Exception:
        logger.exception("Error listando beneficiarios huérfanos")
        return _error_json(500, "No se pudo generar la lista.")


class HuerfanosEliminarIn(BaseModel):
    pais: str
    persona_ids: list[int]


@app.post("/api/huerfanos/eliminar")
def api_huerfanos_eliminar(request: Request, datos: HuerfanosEliminarIn):
    if not _usuario_autenticado(request):
        return _respuesta_no_autenticado()
    if datos.pais not in PAISES_VALIDOS:
        return _error_json(400, "País no válido.")

    user_id = _user_id_sesion(request)
    conn = get_connection(datos.pais)
    try:
        resultado = huerfanos.eliminar(conn.cursor(), datos.persona_ids)
        conn.commit()
        huerfanos.quitar_de_cache(datos.pais, resultado["ids_eliminados"])
        logger.info(
            "Beneficiarios huérfanos eliminados en %s: %s (ids=%s, omitidos=%s, usuario_id=%s)",
            datos.pais, resultado["eliminados"], resultado["ids_eliminados"], resultado["omitidos"], user_id
        )
        return {"status": "ok", **{k: v for k, v in resultado.items() if k != "ids_eliminados"}}
    except huerfanos.HuerfanosError as e:
        conn.rollback()
        return _error_json(400, str(e))
    except IntegrityError:
        conn.rollback()
        logger.exception("Un beneficiario huérfano tenía una referencia inesperada")
        return _error_json(409, "Algún beneficiario tiene un vínculo que no se vio en la lista. No se guardó ningún cambio; recarga la lista.")
    except Exception:
        conn.rollback()
        logger.exception("Error eliminando beneficiarios huérfanos")
        return _error_json(500, "No se pudieron eliminar los beneficiarios. No se guardó ningún cambio.")
    finally:
        conn.close()
