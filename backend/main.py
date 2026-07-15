from fastapi import FastAPI, Request, Query, UploadFile, File, Form, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse,FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import hashlib
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

app = FastAPI(root_path="/carga_masiva")
#app = FastAPI()

# Plantillas y estáticos
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend/templates"))
app.mount("/carga_masiva/static", StaticFiles(directory="frontend/static"), name="static")

# Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajusta esto en producción
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sesiones temporales en memoria
SESSIONS = {}

# ---------------------------------------------------
# RUTAS DE LOGIN Y SESIÓN
# ---------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    session_token = request.cookies.get("session")
    if session_token and session_token in SESSIONS:
        return RedirectResponse(url="/carga", status_code=302)
    return RedirectResponse(url="/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def show_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    hashed_password = hashlib.sha1(password.encode()).hexdigest()
    conn = get_connection("vzla")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT usuario_id FROM psi_usuarios 
        WHERE usuario_username = %s 
          AND usuario_password = %s 
          AND usuario_status = 0 
          AND usuario_organizacion_id = 5
    """, (username, hashed_password))
    result = cursor.fetchone()
    conn.close()

    if result:
        user_id = result[0]
        session_token = f"token-{user_id}"
        SESSIONS[session_token] = username

        response = RedirectResponse(url="/carga", status_code=302)
        response.set_cookie(
            key="session",
            value=session_token,
            httponly=True,
            samesite="lax",
            path="/"
        )
        return response

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
    session_token = request.cookies.get("session")
    usuario = SESSIONS.get(session_token)
    if not session_token or not usuario:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request, "usuario": usuario})

# ---------------------------------------------------
# API DE OPCIONES Y CARGAS
# ---------------------------------------------------

@app.get("/api/opciones")
async def get_opciones(pais: str = Query(...), actividad: str = Query(...)):
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
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...),
    institucion_id: int = Form(...)
):
    conn = None
    try:
        actividad = actividad.lower().strip()

        # ---------------------------------------------------------------------
        # Leer Excel
        # header=1 porque la plantilla tiene encabezados en la fila 2
        # ---------------------------------------------------------------------
        content = await file.read()
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

        print("ERROR GENERAL EN /api/cargar_excel:", str(e))

        return {
            "status": "error",
            "mensaje": str(e)
        }

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
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    conn = None
    cursor = None

    try:
        actividad = actividad.lower().strip()

        content = await file.read()
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

        return {
            "status": "error",
            "mensaje": str(e)
        }

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
    file: UploadFile = File(...),
    pais: str = Form(...),
    actividad: str = Form(...),
    destino_id: int = Form(...)
):
    try:
        actividad = actividad.lower().strip()

        content = await file.read()
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
        return {"status": "error", "mensaje": str(e)}
# Rutas de descarga de archivos de plantilla
@app.get("/descargas/beneficiario", response_class=FileResponse)
def descargar_beneficiario():
    ruta = BASE_DIR / "plantillas" / "CargaMasiva_Beneficiario.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Beneficiario.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/antropometria", response_class=FileResponse)
def descargar_antropometria():
    ruta = BASE_DIR  / "plantillas" / "CargaMasiva_Pesquisa_Antropometrica.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Antropometria.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/descargas/sanguineo", response_class=FileResponse)
def descargar_sanguineo():
    ruta = BASE_DIR  / "plantillas" / "CargaMasiva_Pesquisa_Sanguineo.xlsx"
    return FileResponse(path=ruta, filename="Plantilla_Sanguineo.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
    session_token = request.cookies.get("session")
    usuario = SESSIONS.get(session_token)
    if not session_token or not usuario:
        return RedirectResponse(url="/login", status_code=302)
    

    conn = get_connection(pais)
    cursor = conn.cursor()

    # Obtener organizaciones
    cursor.execute("SELECT organizacion_id, organizacion_nombre FROM psi_organizacion where organizacion_status = 0 and organizacion_id <> 5  ")
    organizaciones = [{"organizacion_id": r[0], "organizacion_nombre": r[1]} for r in cursor.fetchall()]

    # Construir base de consulta
    query = """
        SELECT u.usuario_id, u.usuario_nombre, u.usuario_apellido, u.usuario_username,
               u.usuario_password, u.usuario_status, u.usuario_organizacion_id,
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
        "usuario_password": r[4],
        "usuario_status": r[5],
        "usuario_organizacion_id": r[6],
        "organizacion_nombre": r[7],
    } for r in cursor.fetchall()]

    conn.close()

    return templates.TemplateResponse("usuarios/acciones_basev3.html", {
        "request": request,
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
    session_token = request.cookies.get("session")
    usuario = SESSIONS.get(session_token)
    if not session_token or not usuario:
        return RedirectResponse(url="/login", status_code=302)

    try:
        conn = get_connection(pais)
        cursor = conn.cursor()

        if accion == "bloquear":
            cursor.execute("UPDATE psi_usuarios SET usuario_status = 22 WHERE usuario_id = %s", (usuario_id,))
        elif accion == "cambiar-contrasena":
            hashed = hashlib.sha1(nueva_contrasena.encode()).hexdigest()
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
    buscar: str = ""
):
    # Verificación de sesión
    session_token = request.cookies.get("session")
    usuario = SESSIONS.get(session_token)
    if not session_token or not usuario:
        return RedirectResponse(url="/login", status_code=302)

    conn = get_connection(pais)
    cursor = conn.cursor()

    # Consulta jornadas
    query = """
        SELECT jornada_id, jornada_nombre, jornada_status,  jornada_fecha_inicio
        FROM psi_jornadas
        WHERE 1 = 1
    """
    params = []

    if buscar:
        query += " AND jornada_nombre LIKE %s"
        params.append(f"%{buscar}%")

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
        "pais": pais,
        "buscar": buscar,
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
    session_token = request.cookies.get("session")
    usuario = SESSIONS.get(session_token)
    if not session_token or not usuario:
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
