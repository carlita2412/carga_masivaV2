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
from backend.services.carga_beneficiario import procesar_excel
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
# Los usuarios existentes tienen su contraseña en SHA1 (heredado). Nuevas
# contraseñas y cualquier login exitoso con hash legado se migran a bcrypt
# de forma transparente.
# ---------------------------------------------------

def _hash_password_bcrypt(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _es_hash_bcrypt(valor: str) -> bool:
    return bool(valor) and valor.startswith(("$2a$", "$2b$", "$2y$"))


def _password_coincide(password: str, hash_almacenado: str) -> bool:
    """Verifica el password contra el hash almacenado, soportando bcrypt y
    el legado SHA1 (comparación en tiempo constante)."""
    if not hash_almacenado:
        return False
    if _es_hash_bcrypt(hash_almacenado):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hash_almacenado.encode("utf-8"))
        except ValueError:
            return False
    # Hash legado SHA1
    sha1_calculado = hashlib.sha1(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(sha1_calculado, hash_almacenado)


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
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_connection("vzla")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT usuario_id, usuario_password FROM psi_usuarios
        WHERE usuario_username = %s
          AND usuario_status = 0
          AND usuario_organizacion_id = 5
    """, (username,))
    result = cursor.fetchone()

    if result and _password_coincide(password, result[1]):
        user_id, hash_almacenado = result

        # Migración transparente de contraseñas legadas (SHA1) a bcrypt.
        if not _es_hash_bcrypt(hash_almacenado):
            nuevo_hash = _hash_password_bcrypt(password)
            cursor.execute(
                "UPDATE psi_usuarios SET usuario_password = %s WHERE usuario_id = %s",
                (nuevo_hash, user_id)
            )
            conn.commit()

        conn.close()

        session_token = _crear_sesion(user_id, username)

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
    return templates.TemplateResponse("login.html", {
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
    return templates.TemplateResponse("index.html", {"request": request, "usuario": usuario})

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
    ruta = BASE_DIR  / "plantillas" / "manual_usuario.docs"
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

    return templates.TemplateResponse("usuarios/acciones_basev3.html", {
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
            hashed = _hash_password_bcrypt(nueva_contrasena)
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
@app.get("/jornadas/v2", response_class=HTMLResponse)
async def gestion_jornadas_v2(
    request: Request,
    pais: str = "vzla",
    buscar: str = "",
    estatus: int = None
):
    # Verificación de sesión
    usuario = _usuario_autenticado(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)

    conn = get_connection(pais)
    cursor = conn.cursor()

    # Consulta jornadas
    query = """
        SELECT jornada_id, jornada_nombre, jornada_status, jornada_fecha_inicio
        FROM psi_jornadas
        WHERE 1 = 1
    """
    params = []

    if buscar:
        query += " AND jornada_nombre LIKE %s"
        params.append(f"%{buscar}%")

    if estatus is not None:
        query += " AND jornada_status = %s"
        params.append(estatus)

    query += " ORDER BY jornada_id DESC"
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

    return templates.TemplateResponse("jornadas/jornadas_basev3.html", {
        "request": request,
        "usuario": usuario,
        "pais": pais,
        "buscar": buscar,
        "estatus": estatus,
        "jornadas": jornadas
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
